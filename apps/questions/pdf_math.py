"""Recover very simple, ruled fractions from digital PDF option geometry.

Only simple Type3 math glyphs around a visible rule or radical are reconstructed.
Ambiguous geometry is left unchanged and remains flagged for review: this is
not a general mathematical OCR engine.
"""
from collections import defaultdict
from statistics import median
import re

import pymupdf as fitz


def is_type3_font(name):
    """Recognize PyMuPDF's current and legacy names for anonymous Type3 fonts."""
    return name.startswith("Type3") or name == "Unnamed-T3"


_SIGNS = {"-", "−", "–", "+", "±"}
_LINE_TOLERANCE = 3  # same tolerance PyMuPDF uses to keep words on one text line


def _fraction_bars(rect, drawings):
    """Return (x0, x1, y) for standalone thin horizontal rules inside ``rect``.

    Only a drawing that *is* a short rule qualifies. A horizontal edge that is
    merely part of a larger path (a letter outline in a watermark, a diagram, a
    table border) is never a fraction bar.
    """
    bars = []
    for drawing in drawings or []:
        items = drawing.get("items", [])
        box = drawing.get("rect")
        if box is None or not 1 <= len(items) <= 2:
            continue
        if any(item[0] not in ("l", "re") for item in items):
            continue
        box = fitz.Rect(box)
        if box.height > 1.6 or not 3 <= box.width <= 120:
            continue
        y = (box.y0 + box.y1) / 2
        if rect.x0 - 1 <= box.x0 and box.x1 <= rect.x1 + 1 and rect.y0 <= y <= rect.y1:
            bars.append((box.x0, box.x1, y))
    return bars


def _wrap_fraction_part(text):
    """Parenthesise a multi-term numerator/denominator so a/b stays unambiguous."""
    return text if re.fullmatch(r"[\w.°%√]+", text, flags=re.UNICODE) else f"({text})"


def _find_fractions(words, bars, unit):
    """Match each rule with a numerator directly above and a denominator directly
    below it. Every condition must hold, otherwise the rule is left alone:

    * both parts lie horizontally *inside* the rule and hug it vertically;
    * some other word on the same text row has the rule at its mid-height (the
      label, an operator, a unit…). A radical overbar or an underline sits at the
      top/bottom edge of its row, never at mid-height;
    * the numerator rises above that row and the denominator hangs below it.
    """
    used, fractions = set(), []
    for x0, x1, y in sorted(bars, key=lambda bar: (bar[2], bar[0])):
        inside = [w for w in words if id(w) not in used
                  and w["box"].x0 >= x0 - 3 and w["box"].x1 <= x1 + 3]
        above = [w for w in inside if w["box"].y1 <= y + 1 and y - w["box"].y1 <= 0.6 * unit]
        below = [w for w in inside if w["box"].y0 >= y - 1 and w["box"].y0 - y <= 0.6 * unit]
        if not above or not below:
            continue
        # Keep exponents/indices that visibly belong to an accepted part.
        for group, pool in ((above, [w for w in inside if w["box"].y1 <= y + 1]),
                            (below, [w for w in inside if w["box"].y0 >= y - 1])):
            grown = True
            while grown:
                grown = False
                for word in pool:
                    if word in group:
                        continue
                    if any(min(word["box"].y1, part["box"].y1) - max(word["box"].y0, part["box"].y0)
                           > 0.3 * min(word["box"].height, part["box"].height) for part in group):
                        group.append(word)
                        grown = True
        parts = {id(w) for w in above + below}
        hosts = [w for w in words if id(w) not in parts and id(w) not in used
                 and (w["box"].x1 <= x0 + 1 or w["box"].x0 >= x1 - 1)
                 and w["box"].y0 + 0.3 * w["box"].height <= y <= w["box"].y1 - 0.2 * w["box"].height]
        if not hosts:
            continue
        host = min(hosts, key=lambda w: min(abs(w["box"].x1 - x0), abs(w["box"].x0 - x1)))
        if (min(w["box"].y0 for w in above) > host["box"].y0 - 0.15 * unit
                or max(w["box"].y1 for w in below) < host["box"].y1 + 0.15 * unit):
            continue
        numerator = "".join(w["text"] for w in sorted(above, key=lambda w: w["box"].x0)).strip()
        denominator = "".join(w["text"] for w in sorted(below, key=lambda w: w["box"].x0)).strip()
        if not numerator or not denominator or len(numerator) > 24 or len(denominator) > 24:
            continue
        used.update(parts)
        fractions.append({
            "box": fitz.Rect(x0, host["box"].y0, x1, host["box"].y1), "fraction": True,
            "text": f"{_wrap_fraction_part(numerator)}/{_wrap_fraction_part(denominator)}",
        })
    return used, fractions


def radical_overbars(drawings, unit, max_stroke_height=1.7):
    """Overbars of *drawn* radical signs: a thin rule whose left end meets the
    top of a tall rising stroke. ``max_stroke_height`` is in text-height units."""
    rules, strokes = [], []
    for drawing in drawings or []:
        items = drawing.get("items", [])
        box = drawing.get("rect")
        if box is None or not 1 <= len(items) <= 3 or any(item[0] != "l" for item in items):
            continue
        box = fitz.Rect(box)
        if len(items) == 1 and box.height <= 1.6 and 3 <= box.width <= 160:
            rules.append(box)
        elif box.width <= 0.9 * unit and 0.6 * unit <= box.height <= max_stroke_height * unit:
            # The rising stroke, alone or joined with its short lead-in strokes.
            strokes.append(box)
    return [rule for rule in rules
            if any(abs(stroke.x1 - rule.x0) <= 1.5 and abs(stroke.y0 - (rule.y0 + rule.y1) / 2) <= 1.5
                   for stroke in strokes)]


def _mark_radicals(words, rect, drawings, unit):
    """Prefix a radicand with ``√`` where a radical sign is *drawn* over it.

    Equation editors draw the radical as strokes rather than a text glyph, so
    ``2√13`` extracts as ``2 13``. Only the exact overbar-meets-rising-stroke
    shape is accepted, and only single-row radicands are marked.
    """
    marked = False
    for rule in radical_overbars(drawings, unit):
        y = (rule.y0 + rule.y1) / 2
        if not (rect.x0 - 1 <= rule.x0 and rule.x1 <= rect.x1 + 1 and rect.y0 <= y <= rect.y1):
            continue
        under = sorted((w for w in words
                        if rule.x0 - 2.5 <= w["box"].x0 < rule.x1 and abs(w["box"].y0 - y) <= 0.4 * unit),
                       key=lambda w: w["box"].x0)
        if not under or under[0]["text"].startswith("√"):
            continue
        if len(under) == 1:
            under[0]["text"] = "√" + under[0]["text"]
        else:
            under[0]["text"] = "√(" + under[0]["text"]
            under[-1]["text"] += ")"
        marked = True
    return marked


def _group_rows(items):
    """Group words into text rows exactly as PyMuPDF does for sorted text."""
    items.sort(key=lambda w: (w["box"].y1, w["box"].x0))
    rows, row, row_box = [], [items[0]], fitz.Rect(items[0]["box"])
    for item in items[1:]:
        box = item["box"]
        if (abs(box.y0 - row_box.y0) <= _LINE_TOLERANCE
                or abs(box.y1 - row_box.y1) <= _LINE_TOLERANCE):
            row.append(item)
            row_box |= box
        else:
            rows.append({"box": row_box, "items": row})
            row, row_box = [item], fitz.Rect(box)
    rows.append({"box": row_box, "items": row})
    return rows


def _is_fragment_row(row):
    """A row made only of short glyph groups (exponents, indices, lone symbols)."""
    return (len(row["items"]) <= 8
            and all(not item.get("fraction") and len(item["text"]) <= 4 for item in row["items"])
            and not any(re.search(r"[^\W\d_]{3,}", item["text"]) for item in row["items"]))


def _merge_fragment_rows(rows, unit):
    """Fold raised/lowered fragment rows back into the text row they overlap.

    Equation editors raise exponents and lower indices by more than a line
    tolerance, so they are emitted as separate lines *before or after* their
    row (``10− J`` … ``18``). Two ordinary text lines never overlap vertically;
    a fragment row that overlaps a full row by at least 40% of its own height
    is part of that row and is re-inserted at its horizontal position.
    """
    merged = False
    mains = [row for row in rows if not _is_fragment_row(row)]
    for row in [row for row in rows if _is_fragment_row(row)]:
        box = row["box"]
        best, best_overlap = None, 0.0
        for main in mains:
            overlap = min(box.y1, main["box"].y1) - max(box.y0, main["box"].y0)
            if (overlap > best_overlap and box.x0 <= main["box"].x1 + 2 * unit
                    and box.x1 >= main["box"].x0 - 2 * unit):
                best, best_overlap = main, overlap
        if best is not None and box.height > 0 and best_overlap >= 0.4 * box.height:
            for item in row["items"]:
                item["raised"] = True
            best["items"].extend(row["items"])
            rows.remove(row)
            merged = True
    return merged


def _row_text(row, unit):
    items = sorted(row["items"], key=lambda w: w["box"].x0)
    text = ""
    for index, item in enumerate(items):
        if not index:
            text = item["text"]
            continue
        previous = items[index - 1]
        gap = item["box"].x0 - previous["box"].x1
        # A lone sign printed directly before a fraction is that fraction's sign;
        # a re-inserted exponent/index touches the glyph it belongs to.
        offset = max(abs(item["box"].y0 - previous["box"].y0), abs(item["box"].y1 - previous["box"].y1))
        glued = ((item.get("fraction") and previous["text"] in _SIGNS and gap <= unit)
                 or (gap < 0.15 * unit and (item.get("raised") or previous.get("raised") or offset > 1.5)))
        if glued:
            text += item["text"]
        else:
            # Translate the horizontal gap into spaces, as PyMuPDF's sorted text
            # does, so widely separated side-by-side choices stay visibly apart
            # (the option parser relies on that spacing).
            width = item["box"].width / max(len(item["text"]), 1)
            spaces = int(round(gap / width)) if width > 0 else 1
            text += " " * max(1, min(12, spaces)) + item["text"]
    return text


def _visual_rows(page, rect, drawings, exclude=()):
    """Words of a region as visual rows, with drawn math made explicit.

    Returns ``(rows, unit)``; ``rows`` is empty when the region has no words.
    Words centred inside an ``exclude`` box (a structured table) are skipped.
    """
    words = []
    for word in page.get_text("words", clip=rect):
        if len(word) >= 5 and isinstance(word[4], str) and word[4].strip():
            box = fitz.Rect(word[:4])
            if box.height > 0 and not any(area.contains((box.tl + box.br) / 2) for area in exclude):
                words.append({"box": box, "text": word[4]})
    if not words:
        return [], 10.0
    unit = median(w["box"].height for w in words)
    _mark_radicals(words, rect, drawings, unit)
    bars = _fraction_bars(rect, drawings)
    used, fractions = _find_fractions(words, bars, unit) if bars else (set(), [])
    rows = _group_rows([w for w in words if id(w) not in used] + fractions)
    _merge_fragment_rows(rows, unit)
    rows.sort(key=lambda row: row["box"].y1)
    return rows, unit


def inline_stacked_fractions(page, rect, drawings):
    """Rebuild region text so two-dimensional math reads in visual order.

    PyMuPDF's flat reading order breaks two common layouts:

    * a stacked fraction's numerator and denominator land on separate lines,
      scrambling nearby options and leaking the numerator into the preceding
      statement (``R/8 inside the cone`` reads ``R`` / ``inside the cone`` / ``8``);
    * exponents and indices raised beyond the line tolerance are emitted as
      their own line, detached from the expression they belong to.

    * a row holding an exponent is cut in two at that exponent, and the halves
      are emitted in the wrong order;
    * a radical drawn as strokes leaves no trace in the text at all.

    Words are grouped into rows with the same top/bottom tolerance PyMuPDF uses,
    so ordinary prose reads exactly as it does natively. A confirmed fraction's
    parts become one ``num/den`` token on its row, fragment rows are folded back
    into the row they overlap, and a drawn radical sign prefixes its radicand
    with ``√``. No word of the source is dropped, and nothing is added except
    the ``/`` and ``√`` that the page itself draws.

    Returns the rebuilt text, or ``None`` when the region has no words.
    """
    rows, unit = _visual_rows(page, rect, drawings)
    if not rows:
        return None
    return "\n".join(_row_text(row, unit) for row in rows)


def math_residue(page, rect, drawings, exclude=()):
    """Count two-dimensional math in a region that text could not linearise.

    Ordinary text rows never overlap vertically. Rows that still overlap after
    fractions, radicals and exponents were resolved are stacked expressions the
    rebuild did not understand (nested fractions, limits, multi-row brackets);
    a radical sign taller than one text row likewise spans a stacked radicand.
    A non-zero result means the extracted text for this region should not be
    trusted on its own and the original rendering should be shown with it.
    """
    rows, unit = _visual_rows(page, rect, drawings, exclude)
    stacked = 0
    for index, row in enumerate(rows):
        for other in rows[index + 1:]:
            a, b = row["box"], other["box"]
            overlap = min(a.y1, b.y1) - max(a.y0, b.y0)
            if (overlap >= 0.2 * min(a.height, b.height)
                    and min(a.x1, b.x1) - max(a.x0, b.x0) > 0):
                stacked += 1
    simple = {tuple(rule) for rule in radical_overbars(drawings, unit)}
    tall = sum(1 for rule in radical_overbars(drawings, unit, 4.0)
               if tuple(rule) not in simple and rect.contains((rule.tl + rule.br) / 2)
               and not any(area.contains((rule.tl + rule.br) / 2) for area in exclude))
    return stacked + tall


def latex_chars(chars):
    chars = sorted(chars, key=lambda c: (c["box"].x0, c["box"].y0))
    out, previous = [], None
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-−–=πθλμωαβγΔ×·().,:[]")
    substitutions = {"−": "-", "–": "-", "π": r"\pi ", "θ": r"\theta ", "λ": r"\lambda ",
                     "μ": r"\mu ", "ω": r"\omega ", "α": r"\alpha ", "β": r"\beta ",
                     "γ": r"\gamma ", "Δ": r"\Delta ", "×": r"\times ", "·": r"\cdot "}
    for char in chars:
        value = char["text"]
        if "latex" in char:
            out.append(char["latex"])
            previous = char
            continue
        if value not in allowed:
            return None
        value = substitutions.get(value, value)
        if previous and char["size"] < previous["size"] * .83:
            delta = char["y"] - previous["y"]
            if delta > 1:
                value = "_{" + value + "}"
            elif delta < -1:
                value = "^{" + value + "}"
        out.append(value)
        if char["size"] >= (previous["size"] * .83 if previous else 0):
            previous = char
    return "".join(out)


def reconstruct_option_fractions(page, segment, options, marker_pattern, normalize, drawings):
    """Return option-key -> LaTeX only where every glyph is accounted for."""
    keys = {o["key"] for o in options}
    glyphs, markers, label_ids = [], [], set()
    for block in page.get_text("rawdict", clip=segment.rect).get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            chars = []
            for span in line.get("spans", []):
                for char in span.get("chars", []):
                    item = {"text": char.get("c", ""), "box": fitz.Rect(char["bbox"]),
                            "y": char["origin"][1], "size": span["size"], "font": span["font"], "flags": span.get("flags", 0)}
                    chars.append(item)
                    glyphs.append(item)
            text = "".join(c["text"] for c in chars)
            stripped = text.lstrip()
            match = marker_pattern.match(stripped)
            if not match or not chars:
                continue
            raw = next((value for value in match.groupdict().values() if value), "")
            key = normalize(raw)
            if key not in keys:
                continue
            offset = len(text) - len(stripped)
            relevant = chars[offset:offset + match.end()]
            if not relevant:
                continue
            box = fitz.Rect(relevant[0]["box"])
            for item in relevant[1:]:
                box |= item["box"]
            markers.append((key, raw, box, stripped[match.end():].strip()))
            label_ids.update(id(item) for item in relevant)
    if any(raw in "12345१२३४५" for _, raw, _, _ in markers):
        markers = [(key, raw, box, rest) for key, raw, box, rest in markers if raw in "12345१२३४५"]
    if len(markers) != len(keys) or {key for key, _, _, _ in markers} != keys:
        return {}

    def owner(x, y):
        candidates = []
        for key, _, box, _ in markers:
            right = min((other.x0 for _, _, other, _ in markers
                         if other.x0 > box.x0 + 20 and abs(other.y0 - box.y0) < 12), default=segment.rect.x1)
            delta = abs(y - (box.y0 + box.y1) / 2)
            if box.x0 - 3 <= x < right and delta <= 11:
                candidates.append((delta, abs(x - box.x0), key))
        return min(candidates)[2] if candidates else None

    option_glyphs = defaultdict(list)
    for glyph in glyphs:
        if not glyph["text"].strip() or id(glyph) in label_ids:
            continue
        box = glyph["box"]
        key = owner((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
        if key:
            option_glyphs[key].append(glyph)
    bars = defaultdict(list)
    for drawing in drawings:
        rect = drawing.get("rect")
        if not rect or not 3 <= rect.width <= 100 or rect.height > 1.5:
            continue
        x, y = (rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2
        if not segment.rect.contains(fitz.Point(x, y)):
            continue
        key = owner(x, y)
        if key:
            bars[key].append(rect)
    results = {}
    for key in keys:
        candidate_bars = list(bars[key])
        chars = option_glyphs[key]
        if not chars:
            continue
        math_chars = [c for c in chars if is_type3_font(c["font"])]
        trailing_text = [c for c in chars if not is_type3_font(c["font"])]
        if trailing_text:
            # Some PDFs switch back to a text font for a short unit after a
            # Type3 expression (for example N in 50√3 N). Accept only a small,
            # alphabetic suffix entirely to the right, on the same baseline;
            # any intermixed prose or uncertain geometry remains untouched.
            if (not math_chars or len(trailing_text) > 4
                    or any(not c["text"].isalpha() for c in trailing_text)
                    or min(c["box"].x0 for c in trailing_text) < max(c["box"].x1 for c in math_chars) - .5
                    or any(min(abs(c["y"] - m["y"]) for m in math_chars) > 2 for c in trailing_text)):
                continue
        # Reconstruct a short radical only when every source character under
        # its overbar is accounted for. Some PDFs draw the overbar as one
        # vector rule; others emit one raised Type3 dash per radicand glyph.
        radicals = [c for c in chars if c["text"] == "√"]
        removed, root_tokens, radical_rules = set(), [], set()
        for root in radicals:
            box = root["box"]
            bases = sorted((c for c in chars if c is not root and c["text"].isalnum()
                            and c["box"].x0 >= box.x1 - 1 and abs(c["y"] - root["y"]) <= 2),
                           key=lambda c: c["box"].x0)
            if not bases or bases[0]["box"].x0 - box.x1 > 3:
                break
            contiguous = [bases[0]]
            for base in bases[1:]:
                if len(contiguous) >= 4 or base["box"].x0 - contiguous[-1]["box"].x1 > 2:
                    break
                contiguous.append(base)
            rules = [bar for bar in candidate_bars
                     if abs(bar.x0 - bases[0]["box"].x0) <= 2
                     and bar.x1 >= bases[0]["box"].x1 - 1.5
                     and bases[0]["box"].y0 - 2 <= (bar.y0 + bar.y1) / 2 <= bases[0]["box"].y0 + 1]
            accents, selected = [], []
            if len(rules) == 1:
                bar = rules[0]
                selected = [base for base in contiguous if base["box"].x1 <= bar.x1 + 1.5]
                if not selected or bar.x1 > selected[-1]["box"].x1 + 3:
                    break
                radical_rules.add(id(bar))
            elif not rules:
                for base in contiguous:
                    matches = [c for c in chars if c["text"] in {"-", "–", "−"}
                               and c["y"] < base["y"] - 2
                               and abs(c["box"].x0 - base["box"].x0) <= 1.5
                               and c["box"].x1 >= base["box"].x1 - 1.5
                               and id(c) not in {id(a) for a in accents}]
                    if len(matches) != 1:
                        break
                    selected.append(base)
                    accents.append(matches[0])
                if not selected:
                    break
            else:
                break
            radicand = latex_chars(selected)
            if not radicand:
                break
            combined = fitz.Rect(box)
            for base in selected:
                combined |= base["box"]
            root_tokens.append({"text": "√", "latex": r"\sqrt{" + radicand + "}", "box": combined,
                                "y": root["y"], "size": root["size"], "font": root["font"]})
            removed.add(id(root))
            removed.update(id(c) for c in selected + accents)
        if len(root_tokens) != len(radicals):
            continue
        chars = [c for c in chars if id(c) not in removed] + root_tokens
        candidate_bars = [bar for bar in candidate_bars if id(bar) not in radical_rules]
        fractions, consumed, valid = [], set(), True
        for bar in sorted(candidate_bars, key=lambda r: (r.x0, r.y0)):
            top, bottom = [], []
            y = (bar.y0 + bar.y1) / 2
            for glyph in chars:
                box = glyph["box"]
                x, cy = (box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2
                if bar.x0 - 1 <= x <= bar.x1 + 1 and abs(cy - y) <= 11:
                    (top if cy < y else bottom).append(glyph)
            if not top or not bottom or any(id(c) in consumed for c in top + bottom):
                valid = False
                break
            numerator, denominator = latex_chars(top), latex_chars(bottom)
            if not numerator or not denominator:
                valid = False
                break
            consumed.update(id(c) for c in top + bottom)
            fractions.append((bar.x0, r"\frac{" + numerator + "}{" + denominator + "}"))
        if not valid or not (fractions or root_tokens):
            continue
        remaining = [c for c in chars if id(c) not in consumed]
        if any(latex_chars([c]) is None for c in remaining):
            continue
        events = [(c["box"].x0, c) for c in remaining] + fractions
        events.sort(key=lambda item: item[0])
        pieces, run = [], []
        for _, event in events:
            if isinstance(event, dict):
                run.append(event)
            else:
                if run:
                    pieces.append(latex_chars(run)); run = []
                pieces.append(event)
        if run:
            pieces.append(latex_chars(run))
        if any(piece is None for piece in pieces):
            continue
        latex = "".join(pieces)
        latex = re.sub(r"(?<![A-Za-z])ln(?![A-Za-z])", r"\\ln ", latex)
        latex = re.sub(r"(?<![A-Za-z])and(?![A-Za-z])", r"\\text{ and }", latex)
        if latex and len(latex) <= 1000:
            results[key] = r"\(" + latex + r"\)"
    if results:
        # PDF's flat text ordering can leak a denominator into the next option
        # even when that next option is ordinary text. Repair only a complete
        # one-line option whose source glyphs agree with the inline text, or a
        # short all-Type3 math run. Leave every ambiguous option unchanged.
        for key, _, _, inline in markers:
            if key in results or bars[key]:
                continue
            chars = option_glyphs[key]
            if not chars or any(c["text"] == "√" for c in chars):
                continue
            ordered = sorted(chars, key=lambda c: c["box"].x0)
            raw = "".join(c["text"] for c in ordered)
            if inline and re.sub(r"\s+", "", inline) == re.sub(r"\s+", "", raw):
                results[key] = inline
            elif len(chars) <= 15 and all(is_type3_font(c["font"]) for c in chars):
                value = latex_chars(chars)
                if value:
                    results[key] = r"\(" + value + r"\)"
    return results
