"""Conservative source-geometry repair for wrapped, side-by-side choices.

Flat PDF reading order often interleaves continuation lines from two columns.
Only reorder complete option runs when every visible token is accounted for;
ambiguous, mathematical, and cross-segment cases remain unchanged for review.
"""
from collections import Counter
import re

import pymupdf as fitz


def _tokens(text):
    return Counter(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))


def reorder_wrapped_options(page, segment, markers, options, textpage=None):
    """Return repaired option text by key, or {} if geometry is uncertain."""
    keys = [option.get("key") for option in options]
    if (len(keys) < 2 or len(markers) != len(keys)
            or {key for key, _ in markers} != set(keys) or len(set(keys)) != len(keys)):
        return {}
    rows = []
    for marker in sorted(markers, key=lambda item: (item[1].y0, item[1].x0)):
        if rows and abs(marker[1].y0 - rows[-1][0][1].y0) <= 5:
            rows[-1].append(marker)
        else:
            rows.append([marker])
    rows = [sorted(row, key=lambda item: item[1].x0) for row in rows]
    if not any(len(row) >= 2 for row in rows):
        return {}  # Standard single-column answer runs need no spatial repair.
    # If labels overlap or are printed too close together, column boundaries
    # cannot safely assign their continuation lines.
    if any(any(right[1].x0 - left[1].x1 < 15 for left, right in zip(row, row[1:])) for row in rows):
        return {}
    try:
        kwargs = {"textpage": textpage} if textpage is not None else {"clip": segment.rect}
        words = page.get_text("words", sort=True, **kwargs)
    except Exception:
        return {}
    per_key = {key: [] for key in keys}
    continuation_columns = set()
    for row_index, row in enumerate(rows):
        top = min(box.y0 for _, box in row) - 2
        next_top = min(box.y0 for _, box in rows[row_index + 1]) - 2 if row_index + 1 < len(rows) else segment.rect.y1
        bottom = min(next_top, segment.rect.y1)
        for column_index, (key, marker) in enumerate(row):
            left = marker.x0 - 3
            right = row[column_index + 1][1].x0 - 3 if column_index + 1 < len(row) else segment.rect.x1
            if right - left < 20 or bottom <= top:
                return {}
            for word in words:
                if len(word) < 5 or not isinstance(word[4], str) or not word[4].strip():
                    continue
                box = fitz.Rect(word[:4])
                center = (box.tl + box.br) / 2
                if not (left <= center.x < right and top <= center.y < bottom and segment.rect.contains(center)):
                    continue
                # Remove only the actual selected marker, not references like
                # "Both (2) and (3)" within an answer.
                if any((box & label).get_area() >= box.get_area() * .65 for _, label in markers):
                    continue
                per_key[key].append((box.y0, box.x0, word[4]))
                if box.y0 > marker.y0 + 6:
                    continuation_columns.add(key)
    if not continuation_columns or any(not per_key[key] for key in keys):
        return {}
    repaired = {}
    for key, pieces in per_key.items():
        # Group baselines to preserve left-to-right order despite small font or
        # OCR bounding-box variations within one visual line.
        line_groups = []
        for piece in sorted(pieces, key=lambda item: (item[0], item[1])):
            if line_groups and abs(piece[0] - line_groups[-1][0][0]) <= 3:
                line_groups[-1].append(piece)
            else:
                line_groups.append([piece])
        repaired[key] = " ".join(piece[2] for line in line_groups for piece in sorted(line, key=lambda item: item[1]))
    old_text = " ".join(str(option.get("text", "")) for option in options)
    new_text = " ".join(repaired[key] for key in keys)
    if _tokens(old_text) != _tokens(new_text):
        return {}  # Never remove or invent option content during reordering.
    return repaired


def recover_column_major_grid(page, segment, markers, options, marker_pattern,
                              normalize, style_for, textpage=None):
    """Recover A/C over B/D choices only from a complete two-column grid.

    Some PDFs visually order options down columns while their flat text layer
    reads across rows. Require all four printed labels, rectangular alignment,
    and exact conservation of the existing option tokens. Never infer missing
    labels or content; return no change whenever geometry or text is doubtful.
    """
    if [option.get("key") for option in options] != ["A", "B"]:
        return None
    known = dict(markers)
    if set(known) != {"A", "B"}:
        return None
    a, b = known["A"], known["B"]
    if abs(a.x0 - b.x0) > 6 or b.y0 - a.y0 < 10:
        return None
    try:
        kwargs = {"textpage": textpage} if textpage is not None else {"clip": segment.rect}
        words = page.get_text("words", sort=True, **kwargs)
    except Exception:
        return None
    candidates = []
    for word in words:
        if len(word) < 5 or not isinstance(word[4], str):
            continue
        match = marker_pattern.fullmatch(word[4].strip())
        if not match:
            continue
        raw = next((value for value in match.groupdict().values() if value), "")
        box = fitz.Rect(word[:4])
        if segment.rect.contains((box.tl + box.br) / 2):
            candidates.append((normalize(raw), style_for(raw), word[4].strip(), box))
    first = next((item for item in candidates if item[0] == "A" and (item[3] & a).get_area() > 0), None)
    if not first:
        return None
    styles = first[1]
    top = [item for item in candidates if item[0] == "C" and item[1] == styles
           and abs(item[3].y0 - a.y0) <= 5 and item[3].x0 - a.x1 >= 35]
    bottom = [item for item in candidates if item[0] == "D" and item[1] == styles
              and abs(item[3].y0 - b.y0) <= 5 and item[3].x0 - b.x1 >= 35]
    pairs = [(c, d) for c in top for d in bottom if abs(c[3].x0 - d[3].x0) <= 6]
    if len(pairs) != 1:
        return None
    c, d = pairs[0]
    boxes = {"A": a, "B": b, "C": c[3], "D": d[3]}
    top_y = min(a.y0, c[3].y0) - 2
    mid_y = min(b.y0, d[3].y0) - 2
    divider = min(c[3].x0, d[3].x0) - 3
    pieces = {key: [] for key in boxes}
    for word in words:
        if len(word) < 5 or not isinstance(word[4], str) or not word[4].strip():
            continue
        box = fitz.Rect(word[:4])
        center = (box.tl + box.br) / 2
        if not (segment.rect.contains(center) and center.y >= top_y and center.x >= a.x0 - 3):
            continue
        if any((box & marker).get_area() >= box.get_area() * .65 for marker in boxes.values()):
            continue
        if box.x0 < divider < box.x1:
            return None
        key = ("A" if center.y < mid_y else "B") if center.x < divider else ("C" if center.y < mid_y else "D")
        pieces[key].append((box.y0, box.x0, word[4]))
    if any(not value for value in pieces.values()):
        return None
    result = {}
    for key, entries in pieces.items():
        lines = []
        for entry in sorted(entries, key=lambda item: (item[0], item[1])):
            if lines and abs(entry[0] - lines[-1][0][0]) <= 3:
                lines[-1].append(entry)
            else:
                lines.append([entry])
        result[key] = " ".join(item[2] for line in lines for item in sorted(line, key=lambda item: item[1]))
    previous = " ".join(str(option.get("text", "")) for option in options)
    for label in (c[2], d[2]):
        previous, replacements = re.subn(r"(?<!\w)" + re.escape(label) + r"(?!\w)", "", previous, count=1)
        if replacements != 1:
            return None
    if _tokens(previous) != _tokens(" ".join(result[key] for key in "ABCD")):
        return None
    return ([{"key": key, "text": result[key]} for key in "ABCD"],
            [(key, boxes[key]) for key in "ABCD"])
