"""Layout-aware extraction for digital, numbered question-paper PDFs.

This is deliberately conservative: PDF text is not a semantic document, and
reading order, mathematical layout and illustrations cannot always be inferred
reliably.  Ambiguities are returned as review issues rather than guessed away.
Scanned pages can use a local Tesseract text layer supplied by the caller;
all OCR and geometrical guesses remain subject to human review.
"""

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from statistics import median
import re
import unicodedata

import pymupdf as fitz

from .pdf_tables import borderless_table_from_pdf, repair_simple_superscripts, source_box, table_from_pdf
from .pdf_math import is_type3_font, reconstruct_option_fractions
from .pdf_graphics import bounded_ink_candidate, scanned_graphic_candidates
from .pdf_options import reorder_wrapped_options


PREFIXED_QUESTION = re.compile(
    r"^(?:(?:Q(?:uestion)?\.?\s*(\d+)[:.)]?)|(?:प्रश्न\s*(\d+)[:.)]?)|(?:प्र\.\s*(\d+)[:.)]?))(?=\s|$)",
    re.IGNORECASE,
)
NUMBERED_QUESTION = re.compile(r"^([1-9१-९]\d{0,3})[.)](?=\s|$)")
# Support common printed labels without treating decimal numbers or arbitrary
# parenthesised references in prose as answers. Parsing still requires an
# ordered sequence starting at A / 1.
OPTION_MARKER = re.compile(
    r"\((?P<paren>[A-Ea-e1-5कखगघङ१२३४५])\)"
    r"|\[(?P<bracket>[A-Ea-e1-5कखगघङ१२३४५])\]"
    r"|(?<!\S)(?P<dot>[A-Ea-e1-5१२३४५])\.(?=\s|$)"
    r"|(?<!\S)(?P<right>[A-Ea-e1-5१२३४५])\)(?=\s|$)"
)
OPTION_AT_START = re.compile(r"^\s*(?:\([A-Ea-e1-5कखगघङ१२३४५]\)|\[[A-Ea-e1-5कखगघङ१२३४५]\]|[A-Ea-e1-5१२३४५][.)](?=\s|$))")
FIGURE_REFERENCE = re.compile(r"\b(?:figure|diagram|illustration|graph)\b|\bas shown\b|चित्र|आरेख|ग्राफ", re.IGNORECASE)
TABLE_REFERENCE = re.compile(
    r"\b(?:match\s+(?:the\s+)?(?:list|column)|(?:list|column)[-\s]*[i1]+|table)\b"
    r"|तालिका|सारणी",
    re.IGNORECASE,
)


def _option_raw(match):
    return next((value for value in match.groupdict().values() if value), "")


def _option_style(raw):
    if raw in "12345१२३४५":
        return "numeric"
    if raw in "कखगघङ":
        return "hindi"
    return "letter"


def _normalize_option(raw):
    return dict(zip("12345१२३४५कखगघङ", "ABCDEABCDEABCDE")).get(raw, raw.upper())


def _compact(value):
    return re.sub(r"\s+", " ", value).strip()


def _ocr_line_text(spans):
    """Restore clearly separated OCR word spans when no space glyph was emitted.

    Tesseract can emit Hindi words as separate positioned spans but omit the
    whitespace between them. Insert a space only at a substantial source gap
    between word characters; never try to infer the characters themselves.
    """
    parts, previous = [], None
    def word_edge(char):
        return char.isalnum() or unicodedata.category(char).startswith("M")
    for span in spans:
        value = span.get("text", "")
        if not value:
            continue
        box = fitz.Rect(span["bbox"])
        if previous and not previous[0][-1].isspace() and not value[0].isspace():
            prev_value, prev_box = previous
            gap = box.x0 - prev_box.x1
            height = min(prev_box.height, box.height)
            if (word_edge(prev_value[-1]) and word_edge(value[0])
                    and gap > max(1.5, .15 * height)):
                parts.append(" ")
        parts.append(value)
        previous = (value, box)
    return "".join(parts).strip()


@dataclass
class Anchor:
    number: int
    page_index: int
    rect: fitz.Rect
    label: str
    prefixed: bool
    inferred: bool = False


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
            raw = _option_raw(match)
            key = _normalize_option(raw)
            style = _option_style(raw)
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
                if expected == "F":
                    break
            elif candidate[2] == "A":
                break
        # Among equally complete runs, the later one is usually the answer
        # list following a list of statements or a matching table.
        # Four or five labels are both complete runs. Prefer the later one
        # when a longer labelled statement/list precedes the actual answers.
        previous_score = (min(len(best), 4), best[0][0]) if best else (0, -1)
        if (min(len(sequence), 4), sequence[0][0]) > previous_score:
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

    The optional OCR text pages are local, temporary Tesseract results. Source
    crops always come from the original PDF, not the OCR rendering. The caller
    can use a basic fallback when question anchors are not sufficiently clear.
    """

    def __init__(self, doc, log, ocr_pages=None, unreadable_pages=None):
        self.doc = doc
        self.log = log
        # Keep the Page alive with each TextPage: PyMuPDF retains only a weak
        # parent reference. Keys are zero-based original page numbers.
        self.ocr_pages = ocr_pages or {}
        self.unreadable_pages = set(unreadable_pages or ())
        self.lines = []
        self.images = []
        self.raster_pages = set()
        self.prefixed = []
        self.numbered = []
        self.edge_counts = Counter()
        self.image_pages = {}
        self._tables_by_page = {}
        self._table_data_cache = {}
        self._drawings_by_page = {}
        self._vectors_by_page = {}
        self.two_columns = False
        self._index_document()

    def _index_document(self):
        for page_index, page in enumerate(self.doc):
            page_lines = []
            page_edge_keys = set()
            if page_index in self.ocr_pages:
                ocr_page, textpage = self.ocr_pages[page_index]
                blocks = ocr_page.get_text("dict", textpage=textpage)["blocks"]
            else:
                blocks = page.get_text("dict")["blocks"]
            for block in blocks:
                if block["type"] != 0:
                    continue
                for line in block["lines"]:
                    text = (_ocr_line_text(line["spans"]) if page_index in self.ocr_pages
                            else "".join(span["text"] for span in line["spans"]).strip())
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
            # A full-page raster may already have an invisible OCR text layer.
            # Keep that text, but inspect ink regions rather than attaching the
            # entire scanned page as one question image.
            if any((fitz.Rect(image["bbox"]) & page.rect).get_area() > page.rect.get_area() * .80
                   for image in page_images):
                self.raster_pages.add(page_index)
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

    def _numbered_anchors(self):
        """Find plain numeric question labels only when option evidence exists.

        Numbered instructions, decimal fragments and numbered options can look
        like question labels. Use the outer margin and a sequential option run
        to establish anchors rather than accepting every ``1.``/``1)`` line.
        """
        candidates = []
        repeated_threshold = max(2, int(len(self.doc) * 0.15))
        for anchor in self.numbered:
            page = self.doc[anchor.page_index]
            source_line = next((text for text, box, _ in self.lines[anchor.page_index]
                                if box == anchor.rect), "")
            if re.match(r"^\d+\.\s+\d+\s*$", source_line):
                continue  # e.g. a split decimal 2. 574, not a question
            if self.edge_counts[self._edge_key(page, source_line, anchor.rect)] >= repeated_threshold:
                continue
            candidates.append(anchor)
        if not candidates:
            return []

        # Establish two columns from increasing question numbers at comparable
        # left margins in the two halves, not from arbitrary right-hand options.
        for page_index in {anchor.page_index for anchor in candidates}:
            page = self.doc[page_index]
            left = [a for a in candidates if a.page_index == page_index and a.rect.x0 / page.rect.width < .30]
            right = [a for a in candidates if a.page_index == page_index and .45 <= a.rect.x0 / page.rect.width < .75]
            if left and right:
                lx = min(a.rect.x0 / page.rect.width for a in left)
                rx = min(a.rect.x0 / page.rect.width for a in right)
                lgroup = [a for a in left if abs(a.rect.x0 / page.rect.width - lx) <= .02]
                rgroup = [a for a in right if abs(a.rect.x0 / page.rect.width - rx) <= .02]
                if (len(lgroup) + len(rgroup) >= 3 and abs((rx - .5) - lx) <= .05
                        and min(a.number for a in rgroup) > max(a.number for a in lgroup)):
                    self.two_columns = True
                    break

        def col(anchor):
            return int(self.two_columns and anchor.rect.x0 >= self.doc[anchor.page_index].rect.width / 2)

        def order(anchor):
            return anchor.page_index, col(anchor), anchor.rect.y0

        def region_text(anchor, following):
            # A differently indented numbered question can intervene before the
            # next member of this margin group (notably after a cover page).
            # Do not use that other question's choices to confirm the cover.
            for other in sorted(candidates, key=order):
                if order(other) <= order(anchor) or (following and order(other) >= order(following)):
                    continue
                if other.number > anchor.number:
                    continue
                line = next((text for text, box, _ in self.lines[other.page_index] if box == other.rect), "")
                body = line[len(other.label):].strip()
                question_like = ("?" in body or bool(re.match(
                    r"(?i)^(?:which|what|why|how|find|calculate|choose|identify|select)\b|^(?:क्या|कौन|किस)", body)))
                if question_like:
                    following = other
                    break
            # Read up to the next question in column order, including a bounded
            # continuation into the next column/page. This also lets a question
            # at the end of a page establish confidence from its actual choices.
            start_page, start_col, _ = order(anchor)
            last_page = min(len(self.doc) - 1, start_page + 1,
                            following.page_index if following else len(self.doc) - 1)
            pieces = []
            for page_index in range(start_page, last_page + 1):
                page = self.doc[page_index]
                page_anchors = [a for a in candidates if a.page_index == page_index]
                top, bottom = self._content_bounds(page_index, page_anchors)
                for column in range(2 if self.two_columns else 1):
                    if page_index == start_page and column < start_col:
                        continue
                    if following and (page_index, column) > (following.page_index, col(following)):
                        break
                    x0 = page.rect.width * column / 2 if self.two_columns else 0
                    x1 = page.rect.width * (column + 1) / 2 if self.two_columns else page.rect.width
                    y0 = max(top, anchor.rect.y0 - 5) if (page_index, column) == (start_page, start_col) else top
                    y1 = min(bottom, following.rect.y0 - 5) if following and (page_index, column) == (following.page_index, col(following)) else bottom
                    if y1 > y0:
                        pieces.append(self._text_in_rect(page_index, fitz.Rect(x0, y0, x1, y1)))
                    if following and (page_index, column) == (following.page_index, col(following)):
                        break
            body = "\n".join(pieces)
            return re.sub(r"(?<!\w)" + re.escape(anchor.label.strip()) + r"(?!\d)", "", body, count=1)

        # Cluster margins instead of assuming the leftmost number in the PDF
        # is a question; a cover can have instructions at a different indent.
        groups = []
        positioned = sorted(((a.rect.x0 / self.doc[a.page_index].rect.width - .5 * col(a), a)
                             for a in candidates), key=lambda pair: pair[0])
        for position, anchor in positioned:
            if groups and abs(position - median(value for value, _ in groups[-1])) <= .015:
                groups[-1].append((position, anchor))
            else:
                groups.append([(position, anchor)])

        best = []
        alternatives = []
        best_score = (0, 0, 0)
        for group in groups:
            aligned = sorted((anchor for _, anchor in group), key=order)
            # If numeric choices are printed at exactly the question margin,
            # identify an ordered 1,2,... answer run after a question-like line.
            # Reject this interpretation when a candidate itself contains an
            # independent option run (then it may be another question).
            choice_ids = set()
            for index, candidate in enumerate(aligned):
                if candidate.number != 1 or index == 0 or id(candidate) in choice_ids:
                    continue
                previous = aligned[index - 1]
                if (previous.page_index != candidate.page_index or col(previous) != col(candidate)
                        or candidate.rect.y0 - previous.rect.y0 > 180):
                    continue
                previous_line = next((text for text, box, _ in self.lines[previous.page_index]
                                      if box == previous.rect), "")
                remainder = previous_line[len(previous.label):].strip()
                if len(remainder) < 12 and "?" not in remainder:
                    continue
                run = [candidate]
                for later in aligned[index + 1:]:
                    if (later.page_index != candidate.page_index or col(later) != col(candidate)
                            or later.number != len(run) + 1 or later.number > 5
                            or later.rect.y0 - run[-1].rect.y0 > 120):
                        break
                    if len(parse_options(region_text(run[-1], later))[1]) >= 2:
                        break
                    run.append(later)
                if len(run) >= 2:
                    choice_ids.update(id(item) for item in run)
            aligned = [a for a in aligned if id(a) not in choice_ids]
            confirmed = []
            for index, anchor in enumerate(aligned):
                following = aligned[index + 1] if index + 1 < len(aligned) else None
                if len(parse_options(region_text(anchor, following))[1]) >= 2:
                    confirmed.append(anchor)
            score = (len(confirmed), len({a.number for a in confirmed}), len(aligned))
            alternatives.extend(confirmed)
            if score > best_score:
                # Preserve otherwise unsupported questions when their number
                # fills an unambiguous gap in a confirmed sequence. Their
                # content/options are explicitly flagged, not invented.
                selected = []
                for index, anchor in enumerate(aligned):
                    if anchor in confirmed:
                        selected.append(anchor)
                        continue
                    previous = selected[-1] if selected else None
                    following = next((a for a in aligned[index + 1:] if a in confirmed), None)
                    between = (previous and following and anchor.number == previous.number + 1
                               and anchor.number < following.number)
                    first = (not previous and following and anchor.number + 1 == following.number
                             and not any(a.number == anchor.number for a in aligned[index + 1:aligned.index(following)]))
                    line = next((text for text, box, _ in self.lines[anchor.page_index] if box == anchor.rect), "")
                    last = (previous and not following and anchor.number == previous.number + 1
                            and "?" in line)
                    if between or first or last:
                        anchor.inferred = True
                        selected.append(anchor)
                best, best_score = selected, score
        # A paper can shift its question margin between pages. Recover a
        # separately confirmed anchor from another margin only when it extends
        # the existing sequence in reading order without duplicating a number.
        for anchor in sorted(alternatives, key=order):
            if anchor in best or any(item.number == anchor.number for item in best):
                continue
            combined = sorted(best + [anchor], key=order)
            position = combined.index(anchor)
            previous = combined[position - 1] if position else None
            following = combined[position + 1] if position + 1 < len(combined) else None
            fits_before = following and anchor.number + 1 == following.number and (not previous or previous.number < anchor.number)
            fits_after = previous and previous.number + 1 == anchor.number and (not following or anchor.number < following.number)
            if fits_before or fits_after:
                best = combined
        return best

    def _anchors(self):
        if not self.prefixed:
            return self._numbered_anchors()
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

        # Numeric answer labels may align with an explicit Q label. Treat an
        # adjacent 1,2,... run following a question as choices, not new
        # question anchors. The prefixed anchors also bound the run.
        ordered = sorted(self.prefixed + aligned, key=reading_order)
        numeric_choices = set()
        for index, candidate in enumerate(ordered):
            if candidate.prefixed or candidate.number != 1 or index == 0:
                continue
            previous = ordered[index - 1]
            if (previous.page_index != candidate.page_index or column_of(previous) != column_of(candidate)
                    or candidate.rect.y0 - previous.rect.y0 > 180):
                continue
            if not previous.prefixed:
                line = next((text for text, box, _ in self.lines[previous.page_index] if box == previous.rect), "")
                if len(line[len(previous.label):].strip()) < 12 and "?" not in line:
                    continue
            run = [candidate]
            for later in ordered[index + 1:]:
                if (later.prefixed or later.page_index != candidate.page_index
                        or column_of(later) != column_of(candidate)
                        or later.number != len(run) + 1 or later.number > 5
                        or later.rect.y0 - run[-1].rect.y0 > 120):
                    break
                page = self.doc[candidate.page_index]
                column = column_of(candidate)
                x0 = page.rect.width * column / 2 if self.two_columns else 0
                x1 = page.rect.width * (column + 1) / 2 if self.two_columns else page.rect.width
                between = self._text_in_rect(candidate.page_index, fitz.Rect(
                    x0, max(0, run[-1].rect.y0 - 5), x1, later.rect.y0 - 5))
                between = re.sub(r"(?<!\w)" + re.escape(run[-1].label.strip()) + r"(?!\d)", "", between, count=1)
                if len(parse_options(between)[1]) >= 2:
                    break  # a numbered question with its own choices
                run.append(later)
            if len(run) >= 2:
                numeric_choices.update(id(item) for item in run)
        aligned = [anchor for anchor in aligned if id(anchor) not in numeric_choices
                   and not re.match(r"^\d+\.\s+\d+\s*$", next(
                       (text for text, box, _ in self.lines[anchor.page_index] if box == anchor.rect), ""))]

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

    def _text_in_rect(self, page_index, rect):
        page = self.doc[page_index]
        if page_index not in self.ocr_pages:
            return repair_simple_superscripts(page, rect, page.get_text("text", clip=rect, sort=True))
        # PyMuPDF ignores clip= when a prebuilt OCR TextPage is passed. Filter
        # OCR spans geometrically instead, so one question/column cannot consume
        # the entire page. OCR spans are usually individual words.
        rows = []
        for text, box, spans in self.lines[page_index]:
            if rect.contains(box):
                value = text
            else:
                selected = [span for span in spans
                            if rect.contains((fitz.Rect(span["bbox"]).tl + fitz.Rect(span["bbox"]).br) / 2)]
                value = _ocr_line_text(selected)
            if value:
                rows.append((box.y0, box.x0, value))
        rows.sort(key=lambda row: (row[0], row[1]))
        return "\n".join(row[2] for row in rows)

    def _segment(self, page_index, rect):
        page = self.doc[page_index]
        images = []
        crossing_image = False
        for image in self.images[page_index]:
            image_rect = fitz.Rect(image["bbox"])
            center = (image_rect.tl + image_rect.br) / 2
            # A scan is ordinarily one full-page raster, not a diagram. Never
            # attach that raster to whichever question happens to contain its
            # centre; source-based figure cropping remains available for review.
            if ((page_index in self.ocr_pages and image_rect.get_area() > page.rect.get_area() * 0.35)
                    or (page_index in self.raster_pages and image_rect.get_area() > page.rect.get_area() * 0.80)):
                continue
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
            is_type3_font(span["font"])
            or (typical_size and span["size"] < typical_size * 0.80 and re.search(r"\w", span["text"]))
            for span in spans
        )
        return Segment(
            page_index=page_index,
            rect=rect,
            text=self._text_in_rect(page_index, rect),
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
            f"{'OCR-assisted' if self.ocr_pages else 'Digital'} layout detected: {'two columns' if self.two_columns else 'single column'}; "
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
                    if anchor.inferred:
                        current.issues.append("Question numbering was inferred from nearby labels; verify the source and choices.")
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
        """Replace confidently isolated ruled or text-aligned table regions."""
        has_reference = bool(TABLE_REFERENCE.search(" ".join(texts)))
        # Never consume a grid of answer choices as if it were a question
        # table, even when the statement happens to mention a table elsewhere.
        joined = "\n".join(texts)
        _, preliminary, offset, _ = parse_options(joined)
        first_option_segment = None
        if offset is not None:
            cursor = 0
            for segment_index, value in enumerate(texts):
                if offset < cursor + len(value) + 1:
                    first_option_segment = segment_index
                    break
                cursor += len(value) + 1
        previous_options = question.options
        question.options = preliminary
        markers = self._option_label_boxes(question, joined, offset)
        question.options = previous_options
        output = []
        last_table_segment = None
        last_table_geometry = None
        for index, segment in enumerate(question.segments):
            page = self.doc[segment.page_index]
            if segment.page_index not in self._tables_by_page:
                try:
                    # A raster scan has no usable vector rulings for this
                    # detector; OCR text alone cannot establish a cell grid.
                    self._tables_by_page[segment.page_index] = (
                        [] if segment.page_index in self.ocr_pages else page.find_tables().tables[:100]
                    )
                except Exception:
                    self._tables_by_page[segment.page_index] = []
                    self.log(f"Warning: Page {segment.page_index + 1}: table detection failed; review the original page.")
            candidates = []
            tables = [(table, False) for table in self._tables_by_page[segment.page_index]]
            # Borderless tables have no vector grid. Run text alignment only
            # for explicitly table/matching questions and clip above the first
            # answer, so ordinary grids of options cannot become table cells.
            # OCR-only pages remain too uncertain for this detector.
            if (has_reference and segment.page_index not in self.ocr_pages
                    and segment.page_index not in self.raster_pages
                    and (first_option_segment is None or index <= first_option_segment)):
                clip = fitz.Rect(segment.rect)
                if first_option_segment == index:
                    option_boxes = [rect for _, rect in markers.get(index, [])]
                    if option_boxes:
                        clip.y1 = min(clip.y1, min(rect.y0 for rect in option_boxes) - 1)
                    else:
                        clip.y1 = clip.y0  # Unknown answer boundary: do not guess.
                if clip.height >= 25 and clip.width >= 35:
                    try:
                        text_tables = page.find_tables(clip=clip, strategy="text").tables[:20]
                        tables.extend((table, True) for table in text_tables)
                    except Exception:
                        self.log(f"Warning: Page {segment.page_index + 1}: borderless table detection failed; review the source.")
            for table, borderless in tables:
                box = fitz.Rect(table.bbox)
                # Never silently consume material crossing a question boundary.
                if not (segment.rect + (-1, -1, 1, 1)).contains(box):
                    continue
                # Do not add a text-strategy duplicate of an already accepted
                # ruled grid or replace it with a weaker inferred structure.
                if borderless and any((box & previous[0]).get_area() >= min(box.get_area(), previous[0].get_area()) * .6 for previous in candidates):
                    continue
                cache_key = (segment.page_index, tuple(table.bbox), borderless)
                if cache_key not in self._table_data_cache:
                    try:
                        self._table_data_cache[cache_key] = (borderless_table_from_pdf(page, table)
                                                           if borderless else table_from_pdf(page, table))
                    except Exception:
                        self._table_data_cache[cache_key] = None
                        self.log(f"Warning: Page {segment.page_index + 1}: a table could not be parsed; review the original page.")
                data = deepcopy(self._table_data_cache[cache_key])
                if not data:
                    continue
                if borderless:
                    # Preserve every word in the inferred rectangle. If the
                    # text strategy drops an overflowing/overlapping cell,
                    # retain the original flat text instead of losing it.
                    try:
                        words = page.get_text("words", clip=segment.rect)
                        source_words = [word[4] for word in words if len(word) >= 5
                                        and box.contains((fitz.Rect(word[:4]).tl + fitz.Rect(word[:4]).br) / 2)]
                        tokens = lambda value: Counter(re.findall(r"\w+|[^\w\s]", value, flags=re.UNICODE))
                        if tokens(" ".join(source_words)) != tokens(" ".join(cell["text"] for row in data["rows"] for cell in row)):
                            continue
                    except Exception:
                        continue
                if not has_reference:
                    # An unreferenced grid needs stronger evidence than a
                    # one-row header or a few incidental intersecting rules.
                    values = [cell["text"].strip() for row in data["rows"] for cell in row]
                    if table.row_count < 2 or sum(bool(value) for value in values) < 4:
                        continue
                if first_option_segment is not None:
                    if index > first_option_segment:
                        continue
                    if index == first_option_segment:
                        option_boxes = [rect for _, rect in markers.get(index, [])]
                        # If boundaries cannot be located, do not risk silently
                        # converting answer choices into table cells.
                        if not option_boxes or box.y1 > min(rect.y0 for rect in option_boxes) + 1:
                            continue
                # A horizontal slice must not remove other prose alongside the table.
                outside = False
                for line_text, line_box, _ in self.lines[segment.page_index]:
                    center = (line_box.tl + line_box.br) / 2
                    if segment.rect.contains(center) and box.y0 <= center.y <= box.y1 and not (box + (-2, -2, 2, 2)).contains(center):
                        if line_text.strip():
                            outside = True
                            break
                if not outside and borderless:
                    # A single line box can span both a table cell and prose
                    # beside the table. Check individual words as well, since
                    # replacing a horizontal slice must not remove that prose.
                    for word in words:
                        word_box = fitz.Rect(word[:4])
                        center = (word_box.tl + word_box.br) / 2
                        if (segment.rect.contains(center) and box.y0 <= center.y <= box.y1
                                and not (box + (-2, -2, 2, 2)).contains(center)):
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
                    if borderless:
                        question.issues.append("Borderless table inferred from source alignment; check all cells against the original.")
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

    def _vector_candidates(self, page_index):
        """Return substantial local line-art clusters, excluding ruled tables.

        These are candidates, not a visual understanding model. The caller
        either flags a source-backed crop or leaves it in the review pool.
        """
        if page_index in self._vectors_by_page:
            return self._vectors_by_page[page_index]
        page = self.doc[page_index]
        if page_index in self.ocr_pages:
            self._vectors_by_page[page_index] = []
            return []
        try:
            if page_index not in self._drawings_by_page:
                self._drawings_by_page[page_index] = page.get_drawings()
            drawings = self._drawings_by_page[page_index]
            clusters = page.cluster_drawings(drawings=drawings, x_tolerance=5, y_tolerance=5)
        except Exception:
            self._vectors_by_page[page_index] = []
            self.log(f"Warning: Page {page_index + 1}: vector diagram detection failed; review the original page.")
            return []
        table_boxes = [fitz.Rect(table.bbox) for table in self._tables_by_page.get(page_index, [])]
        result = []
        for box in clusters:
            box = fitz.Rect(box) & page.rect
            if (box.width < 25 or box.height < 25
                    or box.get_area() > page.rect.get_area() * .30):
                continue
            if any((box & table).get_area() >= box.get_area() * .60 for table in table_boxes):
                continue
            expanded = box + (-1, -1, 1, 1)
            item_count = sum(len(drawing.get("items", [])) for drawing in drawings
                             if expanded.contains((drawing["rect"].tl + drawing["rect"].br) / 2))
            if item_count < 3:
                continue  # a box, underline or a pair of axes is not enough
            result.append(box)
        self._vectors_by_page[page_index] = result
        return result

    def _figure_crop_box(self, page_index, segment, rect):
        """Include nearby short source labels without pulling in prose/options."""
        page = self.doc[page_index]
        box = (rect + (-4, -4, 4, 4)) & segment.rect & page.rect
        vicinity = rect + (-14, -14, 14, 14)
        for text, line, _ in self.lines[page_index]:
            if (len(text.strip()) > 12 or OPTION_AT_START.match(text)
                    or PREFIXED_QUESTION.match(text) or NUMBERED_QUESTION.match(text)):
                continue
            center = (line.tl + line.br) / 2
            if vicinity.contains(center) and segment.rect.contains(line):
                box |= line + (-2, -2, 2, 2)
        return box & segment.rect & page.rect

    def _option_label_boxes(self, question, text, first_offset):
        """Locate the chosen option run, including several labels on one line.

        Do not use entire line rectangles as markers: two or more image choices
        may share a line, and a numbered question label itself is not option A.
        If an entire matching run cannot be located, leave figures unassigned.
        """
        result = {index: [] for index in range(len(question.segments))}
        if first_offset is None or not question.options:
            return result
        first_match = OPTION_MARKER.match(text[first_offset:])
        if not first_match:
            return result
        chosen_style = _option_style(_option_raw(first_match))
        keys = [option["key"] for option in question.options]
        candidates = []
        skipped_label = False
        for index, segment in enumerate(question.segments):
            if segment.page_index in self.ocr_pages:
                ocr_page, textpage = self.ocr_pages[segment.page_index]
                blocks = ocr_page.get_text("rawdict", textpage=textpage).get("blocks", [])
            else:
                blocks = self.doc[segment.page_index].get_text("rawdict", clip=segment.rect).get("blocks", [])
            page_candidates = []
            for block in blocks:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    chars = [char for span in line.get("spans", []) for char in span.get("chars", [])]
                    line_text = "".join(char.get("c", "") for char in chars)
                    if not chars or not line_text.strip():
                        continue
                    line_box = fitz.Rect(line["bbox"])
                    if not segment.rect.intersects(line_box):
                        continue
                    # A plain numeric question label would otherwise look like
                    # the first answer in a 1)/2) or 1./2. answer list.
                    stripped = line_text.lstrip()
                    label = NUMBERED_QUESTION.match(stripped)
                    if (index == 0 and not skipped_label and label
                            and label.group(0).strip() == question.label.strip()
                            and line_box.y0 <= segment.rect.y0 + 16):
                        skipped_label = True
                        continue
                    accepted = []
                    for match in OPTION_MARKER.finditer(line_text):
                        raw = _option_raw(match)
                        key = _normalize_option(raw)
                        if _option_style(raw) != chosen_style or key not in keys:
                            continue
                        prefix = line_text[:match.start()].strip()
                        if not accepted:
                            if prefix and (len(prefix) > 30 or re.search(r"[A-Za-z]{3,}", prefix)):
                                continue
                        elif key != chr(ord(accepted[-1]) + 1):
                            continue
                        relevant = chars[match.start():match.end()]
                        if not relevant:
                            continue
                        box = fitz.Rect(relevant[0]["bbox"])
                        for char in relevant[1:]:
                            box |= fitz.Rect(char["bbox"])
                        if segment.rect.contains((box.tl + box.br) / 2):
                            page_candidates.append((index, key, box))
                            accepted.append(key)
            # Sort visually across rows, allowing OCR a few points of baseline
            # variation between side-by-side labels.
            rows = []
            for candidate in sorted(page_candidates, key=lambda item: item[2].y0):
                if rows and abs(candidate[2].y0 - rows[-1][0][2].y0) <= 5:
                    rows[-1].append(candidate)
                else:
                    rows.append([candidate])
            for row in rows:
                candidates.extend(sorted(row, key=lambda item: item[2].x0))
        best = []
        for start, candidate in enumerate(candidates):
            if candidate[1] != keys[0]:
                continue
            sequence = [candidate]
            for later in candidates[start + 1:]:
                if len(sequence) == len(keys):
                    break
                if later[1] == keys[len(sequence)]:
                    sequence.append(later)
                elif later[1] == keys[0]:
                    break
            if len(sequence) == len(keys):
                best = sequence  # last complete run, as in parse_options
        for index, key, box in best:
            result[index].append((key, box))
        return result

    def _finalize(self, question):
        texts = [segment.text for segment in question.segments]
        # The source can place a mathematical accent before Q9 on the same
        # visual line. Remove the label within its segment so offsets still
        # identify the right segment for cross-page figures and options.
        label_pattern = re.compile(r"(?<!\w)" + re.escape(question.label.strip()) + r"(?!\d)")
        for index, segment_text in enumerate(texts):
            updated, count = label_pattern.subn("", segment_text, count=1)
            if count:
                # OCR sometimes splits an adjacent question prefix (for
                # example प्रश्न) and its number into separate text lines.
                # Remove only an isolated, geometrically adjacent prefix.
                segment = question.segments[index]
                if segment.page_index in self.ocr_pages and NUMBERED_QUESTION.match(question.label.strip()):
                    lines = self.lines[segment.page_index]
                    number_line = next((box for value, box, _ in lines
                                        if value.lstrip().startswith(question.label.strip())
                                        and box.y0 <= segment.rect.y0 + 16), None)
                    if number_line:
                        for value, box, _ in lines:
                            prefix = value.strip()
                            if (prefix.lower() in {"q", "q.", "question", "प्रश्न", "प्र."}
                                    and abs(box.y0 - number_line.y0) <= 6
                                    and -2 <= number_line.x0 - box.x1 <= 30):
                                updated = re.sub(r"^\s*" + re.escape(prefix) + r"\s*\n", "", updated, count=1, flags=re.I)
                                break
                texts[index] = updated
                break
        texts = self._structured_table_text(question, texts)
        text = "\n".join(texts)
        question.text, question.options, first_option_offset, ambiguous = parse_options(text)
        # Only geometrically unambiguous ruled fractions are converted to
        # LaTeX. A human still needs to verify every complex-math question.
        reconstructed_by_segment = {}
        for segment_index, segment in enumerate(question.segments):
            if segment.page_index in self.ocr_pages or not segment.has_complex_math or not question.options:
                continue
            try:
                if segment.page_index not in self._drawings_by_page:
                    self._drawings_by_page[segment.page_index] = self.doc[segment.page_index].get_drawings()
                reconstructed = reconstruct_option_fractions(
                    self.doc[segment.page_index], segment, question.options, OPTION_MARKER,
                    _normalize_option, self._drawings_by_page[segment.page_index])
            except Exception:
                self.log(f"Warning: Page {segment.page_index + 1}: math reconstruction failed; review the original page.")
                continue
            reconstructed_by_segment[segment_index] = set(reconstructed)
            for option in question.options:
                if option["key"] in reconstructed:
                    option["text"] = reconstructed[option["key"]]
        question.source_regions = [
            {"page": segment.page_index + 1, "box": source_box(self.doc[segment.page_index], segment.rect)}
            for segment in question.segments if segment.text.strip() or segment.images
        ]
        if any(segment.page_index in self.ocr_pages for segment in question.segments):
            question.issues.append("Local OCR may misread text, formulas, options or diagrams; check the original page.")
        elif any(segment.page_index in self.raster_pages for segment in question.segments):
            question.issues.append("Full-page raster with text layer detected; verify its text, formulas and diagrams against the original.")
        if any(segment.page_index in self.unreadable_pages for segment in question.segments):
            question.issues.append("A scanned page could not be read; the question may be incomplete.")
        if ambiguous:
            question.issues.append("Option boundaries may be ambiguous; compare with the source.")
        if any(segment.has_complex_math for segment in question.segments):
            question.issues.append("Mathematical formatting may be inaccurate; compare with the source.")
        if question.tables:
            question.issues.append("Extracted table structure needs checking against the source, including merged cells and formulas.")
        elif TABLE_REFERENCE.search(question.text):
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
        markers = self._option_label_boxes(question, text, first_option_offset)
        # Flat reading order can interleave wrapped answers printed side by
        # side. Repair only complete, non-mathematical runs contained in one
        # segment, and only when every source token remains accounted for.
        if first_option_segment is not None and question.options:
            segment = question.segments[first_option_segment]
            if not segment.has_complex_math and len(markers[first_option_segment]) == len(question.options):
                ocr = self.ocr_pages.get(segment.page_index)
                page, textpage = ocr if ocr else (self.doc[segment.page_index], None)
                repaired = reorder_wrapped_options(page, segment, markers[first_option_segment], question.options, textpage)
                if repaired and any(option["text"] != repaired[option["key"]] for option in question.options):
                    for option in question.options:
                        option["text"] = repaired[option["key"]]
                    question.issues.append("Wrapped answer text was reordered using source layout; check every option.")
        # Raised numerator/overbar glyphs can sort before the first option
        # label, contaminating the end of the question statement. Remove that
        # short prefix only when source glyphs at the answer row match exactly
        # and every answer on the row was successfully reconstructed.
        if first_option_segment is not None and first_option_offset is not None:
            row_markers = markers[first_option_segment]
            if row_markers:
                first_y = min(box.y0 for _, box in row_markers)
                first_row = [(key, box) for key, box in row_markers if abs(box.y0 - first_y) <= 5]
                prefix = text[:first_option_offset].splitlines()[-1].strip() if text[:first_option_offset].splitlines() else ""
                normalized_prefix = "".join(prefix.split())
                if (normalized_prefix and len(normalized_prefix) <= 30
                        and not re.search(r"[A-Za-z]{2,}", normalized_prefix)
                        and {key for key, _ in first_row} <= reconstructed_by_segment.get(first_option_segment, set())):
                    segment = question.segments[first_option_segment]
                    page = self.doc[segment.page_index]
                    source_chars = []
                    min_x = min(box.x0 for _, box in first_row) - 3
                    for block in page.get_text("rawdict", clip=segment.rect).get("blocks", []):
                        if block.get("type") != 0:
                            continue
                        for line in block.get("lines", []):
                            for span in line.get("spans", []):
                                for char in span.get("chars", []):
                                    box = fitz.Rect(char["bbox"])
                                    if (char.get("c", "").strip() and box.x0 >= min_x
                                            and first_y - 12 <= box.y0 < first_y - .5):
                                        source_chars.append(char["c"])
                    compact_prefix = _compact(prefix)
                    if (Counter(normalized_prefix) == Counter(source_chars)
                            and question.text.endswith(compact_prefix)
                            and len(question.text) > len(compact_prefix)):
                        question.text = question.text[:-len(compact_prefix)].rstrip()

        def option_for(rect, candidates):
            # Require a clear left-to-right column and vertical interval.
            possible = []
            for key, marker in candidates:
                if (rect.x0 < marker.x0 - 3 or rect.y0 < marker.y0 - 3
                        or rect.y0 - marker.y0 > 120):
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

        vector_by_segment = {}
        scan_by_segment = {}
        for index, segment in enumerate(question.segments):
            vectors = []
            for rect in self._vector_candidates(segment.page_index):
                if not segment.rect.contains(rect):
                    continue
                # Do not duplicate a vector overlay already inside an embedded
                # raster illustration.
                if any((rect & image).get_area() >= rect.get_area() * .5 for image in segment.images):
                    continue
                vectors.append(rect)
            vector_by_segment[index] = vectors
            scan_by_segment[index] = []
            if segment.page_index in self.ocr_pages or segment.page_index in self.raster_pages:
                try:
                    scanned = scanned_graphic_candidates(
                        self.doc[segment.page_index], segment.rect, self.lines[segment.page_index])
                    # A vector overlay on a scanned page is also visible to the
                    # pixel detector; do not duplicate the same source figure.
                    scan_by_segment[index] = [rect for rect in scanned if not any(
                        (rect & vector).get_area() >= min(rect.get_area(), vector.get_area()) * .60
                        for vector in vectors)]
                except Exception:
                    self.log(f"Warning: Page {segment.page_index + 1}: scanned diagram detection failed; review the original page.")
        graphic_stems = []
        for index, vectors in vector_by_segment.items():
            first_y = min((rect.y0 for _, rect in markers[index]), default=None)
            for rect in vectors + scan_by_segment[index]:
                if first_option_segment is None or index < first_option_segment or (
                    index == first_option_segment and first_y is not None and rect.y1 < first_y
                ):
                    graphic_stems.append((index, rect))

        main_figures = []
        for index, segment in enumerate(question.segments):
            page = self.doc[segment.page_index]
            page_markers = markers[index]
            first_y = min((rect.y0 for _, rect in page_markers), default=None)
            sources = [(rect, "embedded") for rect in segment.images] + [
                (rect, "vector") for rect in vector_by_segment[index]
            ] + [(rect, "scan") for rect in scan_by_segment[index]]
            for rect, source in sources:
                if rect.get_area() > page.rect.get_area() * 0.55:
                    question.issues.append("Full-page image cannot be assigned as a diagram; compare with the source.")
                    continue
                unknown_position = first_option_segment is not None and index == first_option_segment and first_y is None
                in_options = first_option_segment is not None and (
                    index > first_option_segment
                    or (index == first_option_segment and first_y is not None and rect.y0 >= first_y - 3)
                )
                if source in ("vector", "scan"):
                    rect = self._figure_crop_box(segment.page_index, segment, rect)
                target = None if unknown_position else option_for(rect, page_markers) if in_options else ""
                if source in ("vector", "scan"):
                    description = "vector" if source == "vector" else "scanned-page"
                    question.issues.append(f"Possible {description} diagram detected; check its crop and assignment against the source.")
                    if (target == "" and (not FIGURE_REFERENCE.search(question.text)
                                          or len(graphic_stems) != 1 or main_figures)):
                        target = None
                if in_options or unknown_position:
                    question.issues.append("Possible option image detected; check its assignment against the source.")
                if not segment.rect.contains(rect):
                    target = None
                try:
                    # Bound memory on PDFs with enormous physical page boxes.
                    factor = min(2.0, 2200 / max(rect.width, rect.height))
                    pix = page.get_pixmap(matrix=fitz.Matrix(factor, factor), clip=rect, alpha=False)
                    crop = {"page": segment.page_index + 1, "box": source_box(page, rect),
                            "data": pix.tobytes("png"), "option_key": target, "source": source}
                    if target == "" and not in_options and not unknown_position:
                        main_figures.append(crop)
                    else:
                        question.extra_figures.append(crop)
                except Exception:
                    question.issues.append("Could not save a detected diagram; compare with the source.")
        # Some image-only answers consist of many disconnected small vector
        # strokes or scan fragments. Existing component detectors can discard
        # every piece. Search only inside an unambiguous, empty option cell;
        # preserve the original pixels and flag the assignment for review.
        if first_option_segment is not None:
            for index in range(first_option_segment, len(question.segments)):
                segment = question.segments[index]
                page = self.doc[segment.page_index]
                page_markers = markers[index]
                rows = []
                for marker in sorted(page_markers, key=lambda item: (item[1].y0, item[1].x0)):
                    if rows and abs(marker[1].y0 - rows[-1][0][1].y0) <= 5:
                        rows[-1].append(marker)
                    else:
                        rows.append([marker])
                for row_index, row in enumerate(rows):
                    row.sort(key=lambda item: item[1].x0)
                    next_top = min((box.y0 for _, box in rows[row_index + 1]), default=segment.rect.y1) if row_index + 1 < len(rows) else segment.rect.y1
                    for column_index, (key, marker) in enumerate(row):
                        option = next((option for option in question.options if option["key"] == key), None)
                        if (not option or option["text"].strip()
                                or any(figure["option_key"] == key for figure in question.extra_figures)):
                            continue
                        right = row[column_index + 1][1].x0 - 3 if column_index + 1 < len(row) else segment.rect.x1
                        cell = fitz.Rect(max(segment.rect.x0, marker.x0 - 3), max(segment.rect.y0, marker.y0 - 3),
                                         right, min(segment.rect.y1, next_top - 3, marker.y0 + 180)) & segment.rect
                        rect = bounded_ink_candidate(page, cell, self.lines[segment.page_index])
                        if not rect:
                            continue
                        try:
                            crop = self._figure_crop_box(segment.page_index, segment, rect) & cell
                            factor = min(2.0, 2200 / max(crop.width, crop.height))
                            pix = page.get_pixmap(matrix=fitz.Matrix(factor, factor), clip=crop, alpha=False)
                            question.extra_figures.append({"page": segment.page_index + 1, "box": source_box(page, crop),
                                                           "data": pix.tobytes("png"), "option_key": key, "source": "option-ink"})
                            question.issues.append("Possible disconnected option diagram detected; check its crop and assignment against the source.")
                        except Exception:
                            question.issues.append("Could not save a possible option diagram; compare with the source.")
        # Likewise, if a referenced diagram has no detected components at all,
        # retain disconnected non-text ink from its stem as an unassigned
        # candidate rather than inventing ownership or silently dropping it.
        if FIGURE_REFERENCE.search(question.text) and not main_figures and not graphic_stems:
            for index, segment in enumerate(question.segments):
                if first_option_segment is not None and index > first_option_segment:
                    continue
                region = fitz.Rect(segment.rect)
                if index == first_option_segment:
                    option_boxes = [rect for _, rect in markers[index]]
                    if not option_boxes:
                        continue
                    region.y1 = min(region.y1, min(rect.y0 for rect in option_boxes) - 3)
                page = self.doc[segment.page_index]
                rect = bounded_ink_candidate(page, region, self.lines[segment.page_index])
                if not rect:
                    continue
                try:
                    crop = self._figure_crop_box(segment.page_index, segment, rect) & region
                    factor = min(2.0, 2200 / max(crop.width, crop.height))
                    pix = page.get_pixmap(matrix=fitz.Matrix(factor, factor), clip=crop, alpha=False)
                    question.extra_figures.append({"page": segment.page_index + 1, "box": source_box(page, crop),
                                                   "data": pix.tobytes("png"), "option_key": None, "source": "stem-ink"})
                    question.issues.append("Possible disconnected stem diagram saved to the unassigned pool; verify it against the source.")
                except Exception:
                    question.issues.append("Could not save a possible stem diagram; compare with the source.")
        if main_figures:
            primary = main_figures[0]
            question.image = (primary["page"], primary["data"])
            question.primary_image_region = {"page": primary["page"], "box": primary["box"]}
            question.extra_figures.extend(main_figures[1:])
        if len(main_figures) > 1:
            question.issues.append("Multiple original diagrams detected; check their assignments against the source.")
        if FIGURE_REFERENCE.search(question.text) and not question.image and not any(f["option_key"] == "" for f in question.extra_figures):
            question.issues.append("A figure is referenced but no question diagram was detected.")
