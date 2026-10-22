"""Identity resolution: CRM (account) and person (contact) matched separately.

Tier 0 exact email = auto-link. Anything fuzzier = a *proposal* for a human,
because a false merge (wrong person gets quotes/pricing) is far costlier
than a duplicate record.
"""
import re

GENERIC_LOCAL = {"purchasing", "info", "sales", "hello", "contact", "orders"}
GENERIC_COMPANY_TOKENS = {"upholstery", "interiors", "design", "studio", "group", "co", "inc", "llc",
                          "fabrics", "architects", "the", "and"}


def norm_email(email):
    e = (email or "").strip().lower()
    return e


def email_parts(email):
    e = norm_email(email)
    if "@" not in e:
        return e, ""
    local, domain = e.split("@", 1)
    return local, domain


def name_tokens(name):
    return [t for t in re.split(r"[^a-z]+", (name or "").lower()) if t]


def company_tokens(company):
    toks = [t for t in re.split(r"[^a-z]+", (company or "").lower()) if t]
    return [t for t in toks if t not in GENERIC_COMPANY_TOKENS]


def match(conn, inbound):
    email = norm_email(inbound["email"])
    local, domain = email_parts(email)
    name_toks = name_tokens(inbound["name"])
    surname = name_toks[-1] if name_toks else ""
    first = name_toks[0] if name_toks else ""

    # Tier 0: exact email (primary or known alias)
    row = conn.execute(
        "SELECT c.*, a.name AS acc_name, a.type AS acc_type, a.status AS acc_status "
        "FROM contact_emails ce JOIN crm_contacts c ON c.id=ce.contact_id "
        "LEFT JOIN crm_accounts a ON a.id=c.account_id WHERE ce.email=?", (email,)).fetchone()
    if row:
        return {"tier": "0-exact-email", "outcome": "auto_link", "contact": row, "account_id": row["account_id"],
                "account_status": row["acc_status"], "account_type": row["acc_type"], "account_name": row["acc_name"],
                "signals": ["exact email match %s" % email]}

    # Candidate scan for fuzzy tiers
    contacts = conn.execute(
        "SELECT c.*, a.name AS acc_name, a.type AS acc_type, a.status AS acc_status "
        "FROM crm_contacts c LEFT JOIN crm_accounts a ON a.id=c.account_id").fetchall()
    best = None
    for c in contacts:
        signals, score = [], 0
        c_local, c_domain = email_parts(c["email"] or "")
        if domain and c_domain and domain == c_domain:
            score += 40
            signals.append("same email domain %s" % domain)
        c_toks = name_tokens(c["name"])
        if surname and surname in c_toks:
            score += 25
            signals.append("surname match '%s'" % surname)
        if first and c_toks and first == c_toks[0]:
            score += 20
            signals.append("first name match '%s'" % first)
        elif first and c_local.startswith(first[0]) and surname and surname in c_local:
            score += 15
            signals.append("email local part looks like %s.%s" % (first[0], surname))
        comp_toks = company_tokens(inbound["company"])
        acc_toks = company_tokens(c["acc_name"])
        shared = set(comp_toks) & set(acc_toks)
        if shared:
            score += 20
            signals.append("company token match %s" % ", ".join(sorted(shared)))
        if score > 0 and (best is None or score > best["score"]):
            best = {"contact": c, "score": score, "signals": signals}

    # Account-only match by domain (e.g. a generic purchasing@ inbox at a known firm)
    if best and best["score"] >= 55:
        c = best["contact"]
        return {"tier": "1-fuzzy", "outcome": "proposal", "contact": c, "account_id": c["account_id"],
                "account_status": c["acc_status"], "account_type": c["acc_type"], "account_name": c["acc_name"],
                "signals": best["signals"], "score": best["score"]}

    # Domain-only -> known account, unknown person (new contact under that account)
    if domain and local in GENERIC_LOCAL:
        for c in contacts:
            _, c_domain = email_parts(c["email"] or "")
            if c_domain == domain:
                return {"tier": "1-domain-account", "outcome": "account_only", "contact": None,
                        "account_id": c["account_id"], "account_status": c["acc_status"],
                        "account_type": c["acc_type"], "account_name": c["acc_name"],
                        "signals": ["generic inbox on known account domain %s" % domain]}

    return {"tier": "none", "outcome": "new", "contact": None, "account_id": None,
            "account_status": None, "account_type": None, "account_name": None,
            "signals": ["no CRM match"] if not best else best["signals"]}
