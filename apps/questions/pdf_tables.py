"""Conservative extraction of ruled PDF tables and obvious superscripts.

All data originates in the PDF text layer and line geometry.  Detection is
best-effort; callers keep the review flag and the source-page coordinates.
"""
from collections import Counter
import re

import pymupdf as fitz
from django.core.exceptions import ValidationError

from .structured_content import validate_tables
from .pdf_math import is_type3_font, latex_chars

_SUPER = str.maketrans({"0":"⁰", "1":"¹", "2":"²", "3":"³", "4":"⁴", "5":"⁵", "6":"⁶", "7":"⁷", "8":"⁸", "9":"⁹", "-":"⁻", "−":"⁻", "–":"⁻", "+":"⁺", "t":"ᵗ", "h":"ʰ"})
_SUPER_CHARS = set("0123456789-−–+th")


def repair_simple_superscripts(page, clip, text):
    """Recover unambiguous raised digits/ordinal suffixes, without guessing fractions.

    A small raised span must use the same font as, and start directly after, a
    larger base glyph. Only replace a pattern when all occurrences are backed
    by corresponding source geometry; otherwise leave it as-is for review.
    """
    glyphs, spans = [], []
    for block in page.get_text("rawdict", clip=clip).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                chars = span.get("chars", [])
                value = "".join(c.get("c", "") for c in chars).strip()
                if not value:
                    continue
                record = {"text": value, "size": span["size"], "font": span["font"],
                          "x0": span["bbox"][0], "y": span["origin"][1]}
                spans.append(record)
                for char in chars:
                    if char.get("c", "").strip():
                        glyphs.append({"char": char["c"], "x1": char["bbox"][2],
                                       "y": char["origin"][1], "size": span["size"], "font": span["font"]})
    pairs = Counter()
    for span in spans:
        value = span["text"]
        if not value or any(c not in _SUPER_CHARS for c in value) or is_type3_font(span["font"]):
            continue
        candidates = [g for g in glyphs if g["font"] == span["font"]
                      and 0.60 <= span["size"] / g["size"] <= 0.82
                      and 0.18 * g["size"] <= g["y"] - span["y"] <= 0.65 * g["size"]
                      and -3.0 <= span["x0"] - g["x1"] <= 2.5]
        if not candidates:
            continue
        base = min(candidates, key=lambda g: abs(span["x0"] - g["x1"]))
        pairs[(base["char"], value)] += 1
    for (base, raised), count in pairs.items():
        suffix = r"(?![A-Za-z])" if raised[-1].isalpha() else r"(?![0-9])"
        pattern = re.compile(re.escape(base) + r"\s*" + re.escape(raised) + suffix)
        matches = list(pattern.finditer(text))
        if len(matches) == count:
            text = pattern.sub(lambda _: base + raised.translate(_SUPER), text)
    return text


def _near_index(values, value):
    return min(range(len(values)), key=lambda i: abs(values[i] - value))


def _cluster(values, tolerance=1.0):
    groups = []
    for v in sorted(values):
        if groups and abs(v - sum(groups[-1]) / len(groups[-1])) <= tolerance:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [sum(group) / len(group) for group in groups]



def reconstruct_cell_fraction(page, clip, drawings):
    """Recover a single ruled Type3 fraction only when every glyph is accounted for.

    Cell borders, multi-line prose, mixed fonts and multiple rules are excluded;
    unsupported expressions remain verbatim and the table stays flagged.
    """
    glyphs = []
    for block in page.get_text("rawdict", clip=clip).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    if char.get("c", "").strip():
                        glyphs.append({"text": char["c"], "box": fitz.Rect(char["bbox"]),
                                       "y": char["origin"][1], "size": span["size"], "font": span["font"]})
    if not glyphs or len(glyphs) > 20 or any(not is_type3_font(g["font"]) for g in glyphs):
        return None
    bars = []
    for drawing in drawings:
        bar = drawing.get("rect")
        if (bar and 2.5 <= bar.width <= 100 and bar.height <= 1.5
                and clip.x0 + 1 < bar.x0 < bar.x1 < clip.x1 - 1
                and clip.y0 + 2 < bar.y0 <= bar.y1 < clip.y1 - 2):
            bars.append(bar)
    if len(bars) != 1:
        return None
    bar = bars[0]
    middle = (bar.y0 + bar.y1) / 2
    if any(not (bar.x0 - 1 <= (g["box"].x0 + g["box"].x1) / 2 <= bar.x1 + 1)
           or abs((g["box"].y0 + g["box"].y1) / 2 - middle) > 13 for g in glyphs):
        return None
    top = [g for g in glyphs if (g["box"].y0 + g["box"].y1) / 2 < middle]
    bottom = [g for g in glyphs if (g["box"].y0 + g["box"].y1) / 2 > middle]
    if not top or not bottom or len(top) + len(bottom) != len(glyphs):
        return None
    numerator, denominator = latex_chars(top), latex_chars(bottom)
    if not numerator or not denominator:
        return None
    return r"\(\frac{" + numerator + "}{" + denominator + r"}\)"

def table_from_pdf(page, table):
    """Convert a PyMuPDF table to a grid with merge spans, or reject ambiguity."""
    rows = table.extract()
    if not rows or not (1 <= table.row_count <= 100 and 2 <= table.col_count <= 20):
        return None
    cells = [cell for row in table.rows for cell in row.cells if cell is not None]
    xs = _cluster([v for cell in cells for v in (cell[0], cell[2])])
    ys = _cluster([row.bbox[1] for row in table.rows] + [table.rows[-1].bbox[3]])
    if len(xs) != table.col_count + 1 or len(ys) != table.row_count + 1:
        return None
    clean_rows = []
    drawings = page.get_drawings()
    for row_index, (row, values) in enumerate(zip(table.rows, rows)):
        clean = []
        for col_index, box in enumerate(row.cells):
            if box is None:
                continue
            x0, y0, x1, y1 = box
            colspan = _near_index(xs, x1) - _near_index(xs, x0)
            rowspan = _near_index(ys, y1) - _near_index(ys, y0)
            value = values[col_index] or ""
            value = repair_simple_superscripts(page, fitz.Rect(box), value)
            value = reconstruct_cell_fraction(page, fitz.Rect(box), drawings) or value
            is_header = row_index == 0 and (colspan > 1 or bool(re.search(r"\b(?:column|list)\b", value, re.I)))
            clean.append({"text": value.strip(), "colspan": colspan, "rowspan": rowspan, "header": is_header})
        clean_rows.append(clean)
    result = {"columns": table.col_count, "rows": clean_rows}
    try:
        return validate_tables([result])[0]
    except ValidationError:
        return None


def source_box(page, rect):
    clip = rect & page.rect
    return [round((clip.x0-page.rect.x0)/page.rect.width, 6),
            round((clip.y0-page.rect.y0)/page.rect.height, 6),
            round((clip.x1-page.rect.x0)/page.rect.width, 6),
            round((clip.y1-page.rect.y0)/page.rect.height, 6)]


def borderless_table_from_pdf(page, table):
    """Return a dense text-aligned grid, or None when it is too ambiguous.

    PyMuPDF's text strategy inserts blank spacer rows. Remove only completely
    empty, unmerged rows; do not synthesize cells or infer missing values.
    """
    data = table_from_pdf(page, table)
    if not data or data["columns"] < 2:
        return None
    if any(cell["rowspan"] != 1 or cell["colspan"] != 1
           for row in data["rows"] for cell in row):
        return None
    rows = [row for row in data["rows"] if any(cell["text"].strip() for cell in row)]
    if len(rows) < 3 or any(len(row) != data["columns"] for row in rows):
        return None
    filled = sum(bool(cell["text"].strip()) for row in rows for cell in row)
    if filled / (len(rows) * data["columns"]) < .8:
        return None
    # A single prose line flanked by column text is not sufficient evidence.
    if sum(sum(bool(cell["text"].strip()) for cell in row) >= 2 for row in rows) < 3:
        return None
    try:
        return validate_tables([{"columns": data["columns"], "rows": rows}])[0]
    except ValidationError:
        return None
