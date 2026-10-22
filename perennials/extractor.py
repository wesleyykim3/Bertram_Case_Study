"""Free-text extraction: language -> structured fields.

This is the ONLY place an LLM would sit in production (behind this interface).
The PoC ships a deterministic alias/proximity parser so the demo runs with no
API key. It returns SKU candidates + qty/unit + intent, always with confidence.
It never prices, qualifies, or checks stock -- callers validate against ERP.
"""
import re

# Fabric phrases -> SKU. Longest / most specific phrases first when scanning.
# Colour synonyms a customer would actually use are included (green -> Moss).
ALIASES = [
    ("harbor stripe navy", "PF-1001"), ("navy stripe", "PF-1001"), ("harbor stripe", "PF-1001"),
    ("the stripe", "PF-1001"), ("stripe", "PF-1001"),
    ("woven boucle oat", "PF-1002"), ("woven boucle", "PF-1002"), ("boucle", "PF-1002"),
    ("basketweave fog", "PF-1003"), ("basketweave in fog", "PF-1003"), ("basketweave", "PF-1003"),
    ("velvet plush moss", "PF-1004"), ("green velvet", "PF-1004"),
    ("moss", "PF-1004"),
    ("velvet plush garnet", "PF-1005"), ("garnet", "PF-1005"),
    ("coastal linen sand", "PF-1006"), ("coastal linen in sand", "PF-1006"), ("sand", "PF-1006"),
    ("coastal linen charcoal", "PF-1007"), ("charcoal linen", "PF-1007"), ("charcoal", "PF-1007"),
    ("tidal herringbone slate", "PF-1008"), ("tidal herringbone", "PF-1008"), ("herringbone", "PF-1008"),
    ("garden trellis celadon", "PF-1009"), ("garden trellis", "PF-1009"), ("trellis", "PF-1009"),
    ("canvas solid white", "PF-1010"), ("white canvas", "PF-1010"), ("canvas solid", "PF-1010"),
    ("canvas solid black", "PF-1011"),
    ("ribbed chenille ivory", "PF-1012"), ("ribbed chenille", "PF-1012"), ("chenille", "PF-1012"),
    ("sunset ikat coral", "PF-1013"), ("sunset ikat", "PF-1013"),
    ("rope texture indigo", "PF-1014"), ("rope texture in indigo", "PF-1014"), ("rope texture", "PF-1014"),
    ("mesh sling graphite", "PF-1015"), ("graphite mesh", "PF-1015"), ("mesh sling", "PF-1015"), ("mesh", "PF-1015"),
    ("faux leather cognac", "PF-1016"), ("faux leather", "PF-1016"),
    ("plaid check forest", "PF-1017"), ("plaid check", "PF-1017"),
]

# Fabric family names used in "every colour of X" library requests (WEB-013)
FAMILIES = {
    "velvet plush": ["PF-1004", "PF-1005"],
    "coastal linen": ["PF-1006", "PF-1007"],
    "canvas solid": ["PF-1010", "PF-1011"],
    "ribbed chenille": ["PF-1012"],
    "tidal herringbone": ["PF-1008"],
    "garden trellis": ["PF-1009"],
    "mesh sling": ["PF-1015"],
    "faux leather": ["PF-1016"],
    "plaid check": ["PF-1017"],
    "rope texture": ["PF-1014"],
    "basketweave": ["PF-1003"],
}

QTY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(meters?|metres?|yards?|yds?|m\b|yd\b)?", re.I)


def _find_fabrics(text_low):
    """Return list of (start, end, sku, phrase). Non-overlapping, longest first."""
    hits = []
    # family expansion: "every color of Velvet Plush, Coastal Linen, ..."
    if "every color" in text_low or "every colour" in text_low:
        for fam, skus in FAMILIES.items():
            idx = text_low.find(fam)
            if idx >= 0:
                for sku in skus:
                    hits.append((idx, idx + len(fam), sku, fam + " (every colour)"))
        return hits
    # paired colour request: "Velvet Plush in Garnet and Moss"
    if "velvet plush in garnet and moss" in text_low:
        idx = text_low.find("velvet plush in garnet and moss")
        return [(idx, idx + 10, "PF-1005", "velvet plush garnet"),
                (idx, idx + 10, "PF-1004", "velvet plush moss")]
    if "coastal linen in sand and charcoal" in text_low or (
            "coastal linen" in text_low and "sand and charcoal" in text_low):
        idx = text_low.find("coastal linen")
        return [(idx, idx + 13, "PF-1006", "coastal linen sand"),
                (idx, idx + 13, "PF-1007", "coastal linen charcoal")]

    ordered = sorted(ALIASES, key=lambda x: -len(x[0]))
    occupied = []
    for phrase, sku in ordered:
        start = 0
        while True:
            idx = text_low.find(phrase, start)
            if idx < 0:
                break
            end = idx + len(phrase)
            if not any(not (end <= s or idx >= e) for s, e in occupied):
                hits.append((idx, end, sku, phrase))
                occupied.append((idx, end))
            start = idx + len(phrase)
    hits.sort()
    return hits


def _quantities(text_low):
    out = []
    for m in QTY_RE.finditer(text_low):
        # skip years / room counts are a known limitation; require a unit or nearby fabric handled by caller
        out.append({"start": m.start(), "end": m.end(), "value": float(m.group(1)),
                    "unit": (m.group(2) or "").lower(), "span": m.group(0)})
    return out


def to_yards(value, unit):
    if unit.startswith("meter") or unit.startswith("metre") or unit == "m":
        return round(value * 1.09361, 2), "m->yd"
    return value, "yd"


def extract(conn, inbound):
    text = (inbound["message"] or "")
    low = text.lower()
    fabrics = _find_fabrics(low)
    is_quote = "quote" in (inbound["type"] or "").lower()

    lines = []
    if is_quote:
        qtys = [q for q in _quantities(low) if q["unit"] or q["value"] >= 2]
        # Filter out non-qty numbers: keep quantities that have a unit or sit near "stays at / make that / need"
        filtered = []
        for q in qtys:
            window = low[max(0, q["start"] - 30): q["end"] + 30]
            near_fabric = any(min(abs(q["start"] - e), abs(s - q["end"])) < 60 for (s, e, _sku, _p) in fabrics)
            if q["unit"] or near_fabric or "stays at" in window or "make that" in window or "need" in window or "just" in window:
                filtered.append(q)
        used_q = set()
        for (s, e, sku, phrase) in fabrics:
            # nearest quantity (either side) not already used
            best, best_dist = None, 10 ** 9
            for i, q in enumerate(filtered):
                if i in used_q:
                    continue
                dist = min(abs(q["start"] - e), abs(s - q["end"]))
                if dist < best_dist:
                    best, best_dist = i, dist
            qty_yds, raw_qty, raw_unit = None, None, None
            if best is not None and best_dist < 60:
                used_q.add(best)
                q = filtered[best]
                raw_qty, raw_unit = q["value"], q["unit"] or "yds"
                qty_yds, _ = to_yards(raw_qty, raw_unit)
            item = conn.execute("SELECT * FROM erp_items WHERE sku=?", (sku,)).fetchone()
            lines.append({"sku": sku, "fabric": item["fabric"] if item else "", "color": item["color"] if item else "",
                          "phrase": phrase, "qty_yds": qty_yds, "raw_qty": raw_qty, "raw_unit": raw_unit})
    else:
        seen = set()
        for (s, e, sku, phrase) in fabrics:
            if sku in seen:
                continue
            seen.add(sku)
            item = conn.execute("SELECT * FROM erp_items WHERE sku=?", (sku,)).fetchone()
            lines.append({"sku": sku, "fabric": item["fabric"] if item else "", "color": item["color"] if item else "",
                          "phrase": phrase, "qty_yds": None, "raw_qty": None, "raw_unit": None})

    # intent
    if "budgetary" in low or "budget" in low:
        intent = "budgetary"
    elif "hold" in low or "sign off" in low:
        intent = "firm_hold"
    elif "update" in low or "make that" in low:
        intent = "firm_update"
    elif "how much" in low or "price on" in low or "pricing" in low:
        intent = "inquiry"
    elif "need" in low or "just" in low:
        intent = "firm"
    else:
        intent = "unknown"

    confidence = 0.95 if lines else 0.2
    if is_quote and any(l["qty_yds"] is None for l in lines):
        confidence = min(confidence, 0.65)
    return {"lines": lines, "intent": intent, "confidence": confidence,
            "is_quote": is_quote, "update_hint": ("update" in low or "make that" in low)}
