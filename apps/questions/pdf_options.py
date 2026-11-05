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
