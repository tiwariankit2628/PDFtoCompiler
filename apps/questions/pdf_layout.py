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
            text=page.get_text("text", clip=rect, sort=True),
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
        text = "\n".join(texts)
        question.text, question.options, first_option_offset, ambiguous = parse_options(text)
        if ambiguous:
            question.issues.append("Option boundaries may be ambiguous; compare with the source.")
        if any(segment.has_complex_math for segment in question.segments):
            question.issues.append("Mathematical formatting may be inaccurate; compare with the source.")
        if TABLE_REFERENCE.search(question.text):
            question.issues.append("Table or matching layout may be inaccurate; compare with the source.")
        if any(segment.has_crossing_image for segment in question.segments):
            question.issues.append("A diagram crosses a question or column boundary; check the source.")

        # The local parser automatically chooses at most one question image.
        # Extra/option images are flagged and can be added in the review editor.
        first_option_segment = None
        if first_option_offset is not None:
            offset = 0
            for index, seg_text in enumerate(texts):
                if first_option_offset < offset + len(seg_text) + 1:
                    first_option_segment = index
                    break
                offset += len(seg_text) + 1
        main_figures = []
        option_figures = 0
        for index, segment in enumerate(question.segments):
            page = self.doc[segment.page_index]
            for rect in segment.images:
                if rect.get_area() > page.rect.get_area() * 0.55:
                    question.issues.append("Full-page image cannot be assigned as a diagram; compare with the source.")
                    continue
                in_options = first_option_segment is not None and (
                    index > first_option_segment
                    or (index == first_option_segment and segment.first_option_y is not None and rect.y0 > segment.first_option_y)
                )
                if in_options:
                    option_figures += 1
                else:
                    main_figures.append((segment.page_index, rect))
        if option_figures:
            question.issues.append("Possible option image detected; it was not imported automatically.")
        if len(main_figures) > 1:
            question.issues.append("Multiple diagrams detected; only the first was imported. Check the source.")
        if main_figures:
            page_index, rect = main_figures[0]
            try:
                # Render the original page region rather than regenerate an
                # image; this also preserves masks and overlaid labels.
                pix = self.doc[page_index].get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False)
                question.image = (page_index + 1, pix.tobytes("png"))
            except (RuntimeError, ValueError):
                question.issues.append("Could not save the detected diagram; compare with the source.")
        if FIGURE_REFERENCE.search(question.text) and not question.image:
            question.issues.append("A figure is referenced but no question diagram was detected.")
