"""Layout-aware extraction for digital, numbered question-paper PDFs.

This is deliberately conservative: PDF text is not a semantic document, and
reading order, mathematical layout and illustrations cannot always be inferred
reliably.  Ambiguities are returned as review issues rather than guessed away.
Scanned PDFs are handled by the caller's OCR fallback instead.
"""

from collections import Counter
from dataclasses import dataclass, field
from statistics import median
import re

import pymupdf as fitz

from .pdf_tables import repair_simple_superscripts, source_box, table_from_pdf
from .pdf_math import reconstruct_option_fractions


PREFIXED_QUESTION = re.compile(
    r"^(?:(?:Q(?:uestion)?\.?\s*(\d+)[:.]?)|(?:प्रश्न\s*(\d+)[:.]?)|(?:प्र\.\s*(\d+)[:.]?))(?=\s|$)",
    re.IGNORECASE,
)
NUMBERED_QUESTION = re.compile(r"^([1-9]\d{0,3})[.)](?=\s|$)")
OPTION_MARKER = re.compile(
    r"\((?P<paren>[A-Da-d1-4कखगघ])\)|(?<!\S)(?P<dot>[A-Da-d1-4])\.(?=\s|$)"
)
OPTION_AT_START = re.compile(r"^\s*(?:\([A-Da-d1-4कखगघ]\)|[A-Da-d1-4]\.)")
FIGURE_REFERENCE = re.compile(r"\b(?:figure|diagram|illustration|graph)\b|\bas shown\b|चित्र|आरेख|ग्राफ", re.IGNORECASE)
TABLE_REFERENCE = re.compile(
    r"\b(?:match\s+(?:the\s+)?(?:list|column)|(?:list|column)[-\s]*[i1]+\b|"
    r"(?:given|following|below|above)\s+table\b|table\s+(?:below|above))",
    re.IGNORECASE,
)


def _normalize_option(raw):
    return {"1": "A", "2": "B", "3": "C", "4": "D", "क": "A", "ख": "B", "ग": "C", "घ": "D"}.get(
        raw, raw.upper()
    )


def _compact(value):
    return re.sub(r"\s+", " ", value).strip()


@dataclass
class Anchor:
    number: int
    page_index: int
    rect: fitz.Rect
    label: str
    prefixed: bool


@dataclass
class Segment:
    page_index: int
    rect: fitz.Rect
    text: str
    images: list = field(default_factory=list)
    has_crossing_image: bool = False
    has_complex_math: bool = False
    first_option_y: float = None


@dataclass
class LayoutQuestion:
    number: int
    page_number: int
    label: str
    segments: list = field(default_factory=list)
    text: str = ""
    options: list = field(default_factory=list)
    image: tuple = None  # (source page number, PNG bytes)
    issues: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    source_regions: list = field(default_factory=list)
    extra_figures: list = field(default_factory=list)
    primary_image_region: dict = field(default_factory=dict)


def parse_options(text):
    """Find the last complete sequence of option labels in reading order.

    A matching question may itself contain a list labelled (A)-(D), followed by
    the actual (1)-(4) answers.  Prefer the last complete sequence rather than
    treating the list as the answers.  Parenthesized references *inside* prose
    are not ordinarily treated as labels.  Maths fragments can be positioned
    before a label by the PDF renderer, so a short formula prefix is permitted.
    """
    candidates = []
    offset = 0
    ambiguous = False
    for line in text.splitlines(keepends=True):
        accepted = []
        for match in OPTION_MARKER.finditer(line):
            raw = match.group("paren") or match.group("dot")
            key = _normalize_option(raw)
            style = "numeric" if raw in "1234" else "hindi" if raw in "कखगघ" else "letter"
            prefix = line[:match.start()].strip()
            if not accepted:
                if prefix and (len(prefix) > 30 or re.search(r"[A-Za-z]{3,}", prefix)):
                    continue
            else:
                # Do not confuse a trailing 'a.' in an answer with an option
                # marker when this line uses numeric labels such as (1), (2).
                if style != accepted[0][2] or key != chr(ord(accepted[-1][1]) + 1):
                    continue
                spacing = len(line[:match.start()]) - len(line[:match.start()].rstrip())
                if spacing < 2:
                    ambiguous = True
            accepted.append((match, key, style))
        candidates.extend((offset + m.start(), offset + m.end(), key, style) for m, key, style in accepted)
        offset += len(line)

    best = []
    for index, first in enumerate(candidates):
        if first[2] != "A":
            continue
        sequence = [first]
        expected = "B"
        for candidate in candidates[index + 1:]:
            if candidate[3] != first[3]:
                continue
            if candidate[2] == expected:
                sequence.append(candidate)
                expected = chr(ord(expected) + 1)
                if expected == "E":
                    break
            elif candidate[2] == "A":
                break
        # Among equally complete runs, the later one is usually the answer
        # list following a list of statements or a matching table.
        previous_score = (len(best), best[0][0]) if best else (0, -1)
        if (len(sequence), sequence[0][0]) > previous_score:
            best = sequence

    if not best:
        return _compact(text), [], None, ambiguous
    options = []
    for index, (_, end, key, _) in enumerate(best):
        next_start = best[index + 1][0] if index + 1 < len(best) else len(text)
        options.append({"key": key, "text": _compact(text[end:next_start])})
    return _compact(text[:best[0][0]]), options, best[0][0], ambiguous


class DigitalQuestionLayout:
    """Read numbered digital pages in column order and map embedded figures.

    Only pages with an extractable text layer are supported here.  Existing
    Tesseract handling is retained separately for scanned pages.  The method
    returns None when there are no explicit Q/Question/प्रश्न anchors so the
    caller can use the more general text/OCR fallback.
    """

    def __init__(self, doc, log):
        self.doc = doc
        self.log = log
        self.lines = []
        self.images = []
        self.prefixed = []
        self.numbered = []
        self.edge_counts = Counter()
        self.image_pages = {}
        self._tables_by_page = {}
        self._drawings_by_page = {}
        self.two_columns = False
        self._index_document()

    def _index_document(self):
        for page_index, page in enumerate(self.doc):
            page_lines = []
            page_edge_keys = set()
            for block in page.get_text("dict")["blocks"]:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    text = "".join(span["text"] for span in line["spans"]).strip()
                    if not text:
                        continue
                    rect = fitz.Rect(line["bbox"])
                    page_lines.append((text, rect, line["spans"]))
                    match = PREFIXED_QUESTION.match(text)
                    if match:
                        number = int(next(group for group in match.groups() if group))
                        if number > 0:
                            self.prefixed.append(Anchor(number, page_index, rect, match.group(0), True))
                    else:
                        match = NUMBERED_QUESTION.match(text)
                        if match:
                            self.numbered.append(Anchor(int(match.group(1)), page_index, rect, match.group(0), False))
                    if rect.y0 < page.rect.height * 0.09 or rect.y0 > page.rect.height * 0.87:
                        page_edge_keys.add(self._edge_key(page, text, rect))
            self.edge_counts.update(page_edge_keys)
            self.lines.append(page_lines)
            page_images = page.get_image_info(xrefs=True)
            self.images.append(page_images)
            for image in page_images:
                signature = image.get("digest") or image.get("xref")
                if signature:
                    self.image_pages.setdefault(signature, set()).add(page_index)

        # Questions printed at the left margin of each half are stronger
        # evidence of two columns than arbitrary words/formula fragments.
        left = [a for a in self.prefixed if a.rect.x0 / self.doc[a.page_index].rect.width < 0.30]
        right = [a for a in self.prefixed if 0.45 <= a.rect.x0 / self.doc[a.page_index].rect.width < 0.75]
        self.two_columns = bool(left and right and len(left) + len(right) >= 3)

    @staticmethod
    def _edge_key(page, text, rect):
        return (_compact(text), round(rect.x0 / page.rect.width, 2), round(rect.y0 / page.rect.height, 2))

    def _anchors(self):
        if not self.prefixed:
            return []
        leaders = {}
        for anchor in self.prefixed:
            page = self.doc[anchor.page_index]
            column = int(self.two_columns and anchor.rect.x0 >= page.rect.width / 2)
            leaders.setdefault(column, []).append(anchor.rect.x0 / page.rect.width)
        leaders = {column: median(values) for column, values in leaders.items()}

        def column_of(anchor):
            return int(self.two_columns and anchor.rect.x0 >= self.doc[anchor.page_index].rect.width / 2)

        def reading_order(anchor):
            return anchor.page_index, column_of(anchor), anchor.rect.y0

        aligned = []
        for anchor in self.numbered:
            page = self.doc[anchor.page_index]
            column = column_of(anchor)
            expected = leaders.get(column)
            if expected is None and self.two_columns:
                expected = leaders.get(1 - column, 0.05) + (0.5 if column else -0.5)
            if expected is not None and abs(anchor.rect.x0 / page.rect.width - expected) <= 0.025:
                aligned.append(anchor)

        first_prefixed = min(self.prefixed, key=reading_order)
        anchors = list(self.prefixed)
        for anchor in aligned:
            if reading_order(anchor) >= reading_order(first_prefixed):
                anchors.append(anchor)
                continue
            # If the prefixed sequence starts at Q1, earlier numbered lines
            # are front matter, not earlier questions in that sequence.
            if first_prefixed.number == 1:
                continue
            # Numbered questions can precede prefixed ones in a mixed paper.
            # Keep a leading numbered item only when it contains a plausible
            # option list on this page; otherwise it is likely front matter.
            page = self.doc[anchor.page_index]
            column = column_of(anchor)
            x0 = page.rect.width * column / 2 if self.two_columns else 0
            x1 = page.rect.width * (column + 1) / 2 if self.two_columns else page.rect.width
            following = [
                other.rect.y0 for other in aligned + self.prefixed
                if other.page_index == anchor.page_index and column_of(other) == column
                and other.rect.y0 > anchor.rect.y0 + 1
            ]
            bottom = min(following) - 5 if following else page.rect.height
            clip = fitz.Rect(x0, max(0, anchor.rect.y0 - 5), x1, bottom)
            text = page.get_text("text", clip=clip, sort=True)
            text = re.sub(r"(?<!\w)" + re.escape(anchor.label.strip()) + r"(?!\d)", "", text, count=1)
            if len(parse_options(text)[1]) >= 2:
                anchors.append(anchor)
        return anchors

    def _content_bounds(self, page_index, anchors):
        page = self.doc[page_index]
        top, bottom = 0.0, page.rect.height
        threshold = max(2, int(len(self.doc) * 0.15))
        sizes = [span["size"] for _, _, spans in self.lines[page_index] for span in spans if span["text"].strip()]
        typical_size = median(sizes) if sizes else 10
        first_y = min((anchor.rect.y0 for anchor in anchors), default=0)
        middle = page.rect.width / 2
        for text, rect, spans in self.lines[page_index]:
            repeated = self.edge_counts[self._edge_key(page, text, rect)] >= threshold
            if repeated and rect.y0 < page.rect.height * 0.09:
                top = max(top, rect.y1 + 1)
            elif repeated and rect.y0 > page.rect.height * 0.87:
                bottom = min(bottom, rect.y0 - 1)
            # A large title spanning both columns is not a continuation of
            # the previous question when the right column is read.
            elif (
                self.two_columns and rect.x0 < middle < rect.x1 and rect.y1 < first_y
                and rect.y1 < page.rect.height * 0.25
                and max((span["size"] for span in spans), default=0) > typical_size * 1.3
            ):
                top = max(top, rect.y1 + 1)
        return top, bottom

    def _decorative_image(self, page_index, image):
        page = self.doc[page_index]
        rect = fitz.Rect(image["bbox"])
        signature = image.get("digest") or image.get("xref")
        repeats = len(self.image_pages.get(signature, ())) >= 2
        near_edge = rect.y0 > page.rect.height * 0.86 or rect.y1 < page.rect.height * 0.10
        spanning_watermark = (
            rect.x0 < page.rect.width / 2 < rect.x1
            and rect.width > page.rect.width * 0.30
            and rect.height > page.rect.height * 0.20
        )
        return repeats and (near_edge or spanning_watermark)

    def _segment(self, page_index, rect):
        page = self.doc[page_index]
        images = []
        crossing_image = False
        for image in self.images[page_index]:
            image_rect = fitz.Rect(image["bbox"])
            center = (image_rect.tl + image_rect.br) / 2
            if rect.contains(center) and not self._decorative_image(page_index, image):
                images.append(image_rect)
                crossing_image = crossing_image or not rect.contains(image_rect)

        spans = []
        first_option_y = None
        for text, line_rect, line_spans in self.lines[page_index]:
            center = (line_rect.tl + line_rect.br) / 2
            if rect.contains(center):
                spans.extend(span for span in line_spans if span["text"].strip())
                if OPTION_AT_START.match(text):
                    first_option_y = min(first_option_y, line_rect.y0) if first_option_y is not None else line_rect.y0
        sizes = [span["size"] for span in spans]
        typical_size = median(sizes) if sizes else 0
        complex_math = any(
            span["font"].startswith("Type3")
            or (typical_size and span["size"] < typical_size * 0.80 and re.search(r"\w", span["text"]))
            for span in spans
        )
        return Segment(
            page_index=page_index,
            rect=rect,
            text=repair_simple_superscripts(page, rect, page.get_text("text", clip=rect, sort=True)),
            images=images,
            has_crossing_image=crossing_image,
            has_complex_math=complex_math,
            first_option_y=first_option_y,
        )

    def extract(self):
        anchors = self._anchors()
        if not anchors:
            return None
        by_page = {}
        for anchor in anchors:
            by_page.setdefault(anchor.page_index, []).append(anchor)
        self.log(
            f"Digital layout detected: {'two columns' if self.two_columns else 'single column'}; "
            "matching question labels and figures by their position."
        )
        questions = []
        current = None
        for page_index, page in enumerate(self.doc):
            page_anchors = by_page.get(page_index, [])
            top, bottom = self._content_bounds(page_index, page_anchors)
            # Ignore back covers after a finished question.  A continuation
            # page without anchors is retained if the preceding item is still
            # incomplete (or has not yet been parsed).
            if not page_anchors and current:
                so_far = "\n".join(seg.text for seg in current.segments)
                if len(parse_options(so_far)[1]) >= 4:
                    continue
            for column in range(2 if self.two_columns else 1):
                x0 = page.rect.width * column / 2 if self.two_columns else 0
                x1 = page.rect.width * (column + 1) / 2 if self.two_columns else page.rect.width
                column_anchors = sorted(
                    (a for a in page_anchors if x0 <= a.rect.x0 < x1), key=lambda a: a.rect.y0
                )
                start = top
                for anchor in column_anchors:
                    cut = max(top, min(bottom, anchor.rect.y0 - 5))
                    if current and cut > start:
                        current.segments.append(self._segment(page_index, fitz.Rect(x0, start, x1, cut)))
                    current = LayoutQuestion(anchor.number, page_index + 1, anchor.label)
                    questions.append(current)
                    start = cut
                if current and bottom > start and (page_anchors or current.segments):
                    current.segments.append(self._segment(page_index, fitz.Rect(x0, start, x1, bottom)))

        for question in questions:
            self._finalize(question)
        counts = Counter(question.number for question in questions)
        duplicates = sorted(number for number, count in counts.items() if count > 1)
        if duplicates:
            self.log("Warning: Repeated question numbers detected: " + ", ".join(map(str, duplicates)) + ". Review the numbering.")
            for question in questions:
                if question.number in duplicates:
                    question.issues.append("Duplicate question number; compare with the source.")
        gaps = [
            (previous.number, current.number)
            for previous, current in zip(questions, questions[1:])
            if current.number > previous.number + 1
        ]
        if gaps:
            jumps = ", ".join(f"{start} to {end}" for start, end in gaps[:10])
            self.log(f"Warning: Question numbers jump from {jumps}. Check for missing questions or intentional gaps.")
        return questions

    def _structured_table_text(self, question, texts):
        """Replace only confidently isolated ruled table regions with markers."""
        if not TABLE_REFERENCE.search(" ".join(texts)):
            return texts
        output = []
        last_table_segment = None
        last_table_geometry = None
        for index, segment in enumerate(question.segments):
            page = self.doc[segment.page_index]
            if segment.page_index not in self._tables_by_page:
                try:
                    self._tables_by_page[segment.page_index] = page.find_tables().tables[:100]
                except (ValueError, RuntimeError, AttributeError):
                    self._tables_by_page[segment.page_index] = []
            candidates = []
            for table in self._tables_by_page[segment.page_index]:
                box = fitz.Rect(table.bbox)
                # Never silently consume material crossing a question boundary.
                if not (segment.rect + (-1, -1, 1, 1)).contains(box):
                    continue
                data = table_from_pdf(page, table)
                if not data:
                    continue
                # A horizontal slice must not remove other prose alongside the table.
                outside = False
                for line_text, line_box, _ in self.lines[segment.page_index]:
                    center = (line_box.tl + line_box.br) / 2
                    if segment.rect.contains(center) and box.y0 <= center.y <= box.y1 and not (box + (-2, -2, 2, 2)).contains(center):
                        if line_text.strip():
                            outside = True
                            break
                if not outside:
                    # Track the central divider when a header and body are
                    # separated by a page/column break. Only a geometrically
                    # matching two-cell header and four-cell body can merge.
                    row_cells = table.rows[0].cells if table.rows else []
                    boundary = None
                    mid_index = table.col_count // 2 - 1
                    if table.col_count in (2, 4) and len(row_cells) > mid_index and row_cells[mid_index]:
                        boundary = (row_cells[mid_index][2] - box.x0) / box.width
                    candidates.append((box, data, boundary))
            candidates.sort(key=lambda item: item[0].y0)
            if not candidates:
                output.append(texts[index])
                if texts[index].strip():
                    last_table_segment = None
                continue
            fragments = []
            cursor = segment.rect.y0
            for box, data, boundary in candidates:
                if box.y0 < cursor:
                    continue
                before = fitz.Rect(segment.rect.x0, cursor, segment.rect.x1, box.y0)
                pre = repair_simple_superscripts(page, before, page.get_text("text", clip=before, sort=True)) if before.height > 0 else ""
                # Label was already removed from texts; remove it again from slices.
                pre = re.sub(r"(?<!\w)" + re.escape(question.label.strip()) + r"(?!\d)", "", pre, count=1)
                fragments.append(pre)
                continuation = (last_table_segment is not None and last_table_segment != index
                                and not pre.strip() and question.tables
                                and question.tables[-1]["columns"] == data["columns"])
                merge_header = False
                if (not continuation and last_table_segment is not None and last_table_segment != index
                        and not pre.strip() and question.tables and last_table_geometry and boundary is not None):
                    previous = question.tables[-1]
                    header = previous["rows"][0] if len(previous["rows"]) == 1 else []
                    prior_width, prior_boundary = last_table_geometry
                    merge_header = (previous["columns"] == 2 and data["columns"] == 4
                        and len(header) == 2 and all(cell["header"] and cell["colspan"] == 1 and cell["rowspan"] == 1 for cell in header)
                        and prior_boundary is not None and abs(prior_boundary - boundary) <= .025
                        and abs(prior_width - box.width) / max(prior_width, box.width) <= .05)
                if merge_header:
                    previous["columns"] = 4
                    for cell in header:
                        cell["colspan"] = 2
                    previous["rows"].extend(data["rows"])
                elif continuation:
                    question.tables[-1]["rows"].extend(data["rows"])
                else:
                    question.tables.append(data)
                    fragments.append(f"\n[[table:{len(question.tables)}]]\n")
                cursor = box.y1
                last_table_segment = index
                last_table_geometry = (box.width, boundary)
            after = fitz.Rect(segment.rect.x0, cursor, segment.rect.x1, segment.rect.y1)
            post = repair_simple_superscripts(page, after, page.get_text("text", clip=after, sort=True)) if after.height > 0 else ""
            fragments.append(post)
            if post.strip():
                last_table_segment = None
            output.append("\n".join(fragments))
        return output

    def _finalize(self, question):
        texts = [segment.text for segment in question.segments]
        # The source can place a mathematical accent before Q9 on the same
        # visual line. Remove the label within its segment so offsets still
        # identify the right segment for cross-page figures and options.
        label_pattern = re.compile(r"(?<!\w)" + re.escape(question.label.strip()) + r"(?!\d)")
        for index, segment_text in enumerate(texts):
            updated, count = label_pattern.subn("", segment_text, count=1)
            if count:
                texts[index] = updated
                break
        texts = self._structured_table_text(question, texts)
        text = "\n".join(texts)
        question.text, question.options, first_option_offset, ambiguous = parse_options(text)
        # Only geometrically unambiguous ruled fractions are converted to
        # LaTeX. A human still needs to verify every complex-math question.
        for segment in question.segments:
            if not segment.has_complex_math or not question.options:
                continue
            if segment.page_index not in self._drawings_by_page:
                self._drawings_by_page[segment.page_index] = self.doc[segment.page_index].get_drawings()
            reconstructed = reconstruct_option_fractions(
                self.doc[segment.page_index], segment, question.options, OPTION_MARKER,
                _normalize_option, self._drawings_by_page[segment.page_index])
            for option in question.options:
                if option["key"] in reconstructed:
                    option["text"] = reconstructed[option["key"]]
        question.source_regions = [
            {"page": segment.page_index + 1, "box": source_box(self.doc[segment.page_index], segment.rect)}
            for segment in question.segments if segment.text.strip() or segment.images
        ]
        if ambiguous:
            question.issues.append("Option boundaries may be ambiguous; compare with the source.")
        if any(segment.has_complex_math for segment in question.segments):
            question.issues.append("Mathematical formatting may be inaccurate; compare with the source.")
        if TABLE_REFERENCE.search(question.text):
            if question.tables:
                question.issues.append("Extracted table structure needs checking against the source, including merged cells and formulas.")
            else:
                question.issues.append("Table or matching layout may be inaccurate; compare with the source.")
        if any(segment.has_crossing_image for segment in question.segments):
            question.issues.append("A diagram crosses a question or column boundary; check the source.")

        # Keep all original embedded image crops.  Question images before the
        # answer list are attached; option images are assigned only when their
        # geometry is unambiguous. Other candidates remain in a review pool.
        first_option_segment = None
        if first_option_offset is not None:
            offset = 0
            for index, seg_text in enumerate(texts):
                if first_option_offset < offset + len(seg_text) + 1:
                    first_option_segment = index
                    break
                offset += len(seg_text) + 1
        markers = {}
        for index, segment in enumerate(question.segments):
            found = []
            for line_text, line_rect, _ in self.lines[segment.page_index]:
                center = (line_rect.tl + line_rect.br) / 2
                match = OPTION_MARKER.match(line_text.strip()) if segment.rect.contains(center) else None
                if match:
                    raw = match.group("paren") or match.group("dot")
                    found.append((_normalize_option(raw), raw, line_rect))
            markers[index] = found
        # A numeric answer list is stronger than (A)-(D) labels inside a table.
        numeric = any(raw in "1234" for found in markers.values() for _, raw, _ in found)
        valid_keys = {option["key"] for option in question.options}
        for index in markers:
            markers[index] = [(key, rect) for key, raw, rect in markers[index]
                              if key in valid_keys and (not numeric or raw in "1234")]

        def option_for(rect, candidates):
            # Require a clear left-to-right column and vertical interval.
            possible = []
            for key, marker in candidates:
                if rect.x0 < marker.x0 - 3 or rect.y0 < marker.y0 - 3:
                    continue
                right = min((other.x0 for _, other in candidates
                             if other.x0 > marker.x0 + 20 and abs(other.y0 - marker.y0) < 12), default=float("inf"))
                if rect.x1 > right + 2:
                    continue
                following = [other.y0 for _, other in candidates
                             if abs(other.x0 - marker.x0) < 20 and other.y0 > marker.y0 + 5]
                if following and rect.y1 > min(following) + 2:
                    continue
                possible.append((rect.y0 - marker.y0, abs(rect.x0 - marker.x0), key))
            possible.sort()
            if not possible or (len(possible) > 1 and possible[0][:2] == possible[1][:2]):
                return None
            return possible[0][2]

        main_figures = []
        for index, segment in enumerate(question.segments):
            page = self.doc[segment.page_index]
            page_markers = markers[index]
            first_y = min((rect.y0 for _, rect in page_markers), default=None)
            for rect in segment.images:
                if rect.get_area() > page.rect.get_area() * 0.55:
                    question.issues.append("Full-page image cannot be assigned as a diagram; compare with the source.")
                    continue
                in_options = first_option_segment is not None and (
                    index > first_option_segment
                    or (index == first_option_segment and first_y is not None and rect.y0 > first_y)
                )
                target = option_for(rect, page_markers) if in_options else ""
                if in_options:
                    question.issues.append("Possible option image detected; check its assignment against the source.")
                if not segment.rect.contains(rect):
                    target = None
                try:
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False)
                    crop = {"page": segment.page_index + 1, "box": source_box(page, rect),
                            "data": pix.tobytes("png"), "option_key": target}
                    if target == "" and not in_options:
                        main_figures.append(crop)
                    else:
                        question.extra_figures.append(crop)
                except (RuntimeError, ValueError):
                    question.issues.append("Could not save a detected diagram; compare with the source.")
        if main_figures:
            primary = main_figures[0]
            question.image = (primary["page"], primary["data"])
            question.primary_image_region = {"page": primary["page"], "box": primary["box"]}
            question.extra_figures.extend(main_figures[1:])
        if len(main_figures) > 1:
            question.issues.append("Multiple original diagrams detected; check their assignments against the source.")
        if FIGURE_REFERENCE.search(question.text) and not question.image and not any(f["option_key"] == "" for f in question.extra_figures):
            question.issues.append("A figure is referenced but no question diagram was detected.")
