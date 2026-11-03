"""Recover very simple, ruled fractions from digital PDF option geometry.

Only Type3 math glyphs around an actual horizontal rule are reconstructed. We
reject radical signs, overlaps and ambiguous geometry and keep the review flag:
this is not a general mathematical OCR engine.
"""
from collections import defaultdict
import re

import pymupdf as fitz


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
            raw = match.group("paren") or match.group("dot")
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
    if any(raw in "1234" for _, raw, _, _ in markers):
        markers = [(key, raw, box, rest) for key, raw, box, rest in markers if raw in "1234"]
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
        if not chars or any(not c["font"].startswith("Type3") for c in chars):
            continue
        # A one-character radical is reconstructed only when its overbar is
        # visibly present as either a raised Type3 glyph or a vector rule.
        # More complicated roots are left untouched for human review.
        radicals = [c for c in chars if c["text"] == "√"]
        removed, root_tokens, radical_rules = set(), [], set()
        for root in radicals:
            box = root["box"]
            base_candidates = [c for c in chars if c is not root and c["text"].isalnum()
                               and -1 <= c["box"].x0 - box.x1 <= 3
                               and abs(c["y"] - root["y"]) <= 2]
            if len(base_candidates) != 1:
                break
            base = base_candidates[0]
            accent = [c for c in chars if c["text"] in {"-", "–", "−"} and c.get("flags", 0) & 1
                      and abs(c["box"].x0 - base["box"].x0) <= 1.5
                      and c["y"] < base["y"] - 2]
            rules = [bar for bar in candidate_bars if abs(bar.x0 - base["box"].x0) <= 2
                     and bar.x1 >= base["box"].x1 - 1.5
                     and base["box"].y0 - 2 <= (bar.y0 + bar.y1) / 2 <= base["box"].y0 + 1]
            if len(accent) + len(rules) != 1:
                break
            # The inferred radicand must be one visible character only.
            if rules and any(c is not base and c is not root and c["text"].isalnum()
                             and c["box"].x0 >= base["box"].x1 - .5
                             and c["box"].x0 < rules[0].x1 - .5 and abs(c["y"] - base["y"]) <= 2
                             for c in chars):
                break
            radicand = latex_chars([base])
            if not radicand:
                break
            combined = fitz.Rect(box) | base["box"]
            root_tokens.append({"text": "√", "latex": r"\sqrt{" + radicand + "}", "box": combined,
                                "y": root["y"], "size": root["size"], "font": root["font"]})
            removed.update([id(root), id(base)])
            removed.update(id(c) for c in accent)
            radical_rules.update(id(bar) for bar in rules)
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
            elif len(chars) <= 15 and all(c["font"].startswith("Type3") for c in chars):
                value = latex_chars(chars)
                if value:
                    results[key] = r"\(" + value + r"\)"
    return results
