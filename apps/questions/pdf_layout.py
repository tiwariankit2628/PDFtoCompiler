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
from .pdf_options import recover_column_major_grid, reorder_wrapped_options


PREFIXED_QUESTION = re.compile(
    r"^(?:(?:Q(?:uestion)?\.?\s*(?:No\.{1,2}\s*)?(\d+)[:.)]?)"
    r"|(?:प्रश्न\s*(\d+)[:.)]?)|(?:प्र\.\s*(\d+)[:.)]?))(?=\s|$|\([A-Ea-e]\))",
    re.IGNORECASE,
)
NUMBERED_QUESTION = re.compile(r"^([1-9१-९]\d{0,3})[.)](?=\s|$)")
# OCR can misread the initial Q of "Q. No. 2" as 0 or as the nearby
# question number. Accept only the explicit No. label and flag the inference.
OCR_DAMAGED_QNO = re.compile(
    r"^(?:[0O][.)]|[\[(]?\d{1,4}[.)])\s*No\.{1,2}\s*(\d{1,4})[:.)]?(?=\s|$|\([A-Ea-e]\))",
    re.IGNORECASE,
)
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
# Avoid automatically placing a visibly labelled answer/solution inside a
# source screenshot shown during a later test. Image-only keys can still be
# missed, so every crop must be reviewed before publishing.
ANSWER_OR_SOLUTION = re.compile(
    r"(?:^|\s)(?:ans(?:wer)?\.?|correct\s+answer|solution|explanation|उत्तर|सही\s*उत्तर)"
    r"\s*(?::|=|[-–—]|\s+is\s+(?:\([A-Ea-e1-5]\)|[A-Ea-e1-5](?=\s|[.,;:]|$))|\([A-Ea-e1-5]\))"
    r"|(?:^|\n)\s*(?:solutions?|explanations?)\s*(?:\n|$)",
    re.IGNORECASE,
)
MULTIPLE_CORRECT = re.compile(
    r"\b(?:one\s+or\s+more(?:\s+than\s+one)?|more\s+than\s+one|multiple)\b.{0,100}\b(?:correct|right|true)\b",
    re.IGNORECASE | re.DOTALL,
)
SINGLE_CORRECT = re.compile(
    r"\b(?:only|exactly)\s+one\b.{0,80}\b(?:correct|right|true)\b", re.IGNORECASE | re.DOTALL,
)

TABLE_REFERENCE = re.compile(
    r"\b(?:match\s+(?:the\s+)?(?:list|column)|(?:list|column)[-\s]*[i1]+|table)\b"
    r"|तालिका|सारणी",
    re.IGNORECASE,
)


def _find_geometric_tables(page, **kwargs):
    """Use line/text geometry even if another library activated a layout model.

    Recent PyMuPDF versions otherwise default to gating table detection on
    process-global model classifications. Older supported versions do not have
    this keyword, so retain their original behavior on that specific error.
    """
    try:
        return page.find_tables(use_layout=False, **kwargs)
    except TypeError as exc:
        if not re.search(r"unexpected keyword argument [\"']use_layout[\"']", str(exc)):
            raise
        return page.find_tables(**kwargs)


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
    # Transient geometry for original table screenshots, not a database field.
    table_regions: list = field(default_factory=list)
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
        # The legacy document-wide signal remains a fallback for continuation
        # pages. Per-page evidence prevents a multi-column page from slicing a
        # later full-width page in half.
        self.two_columns = False
        self.column_counts = {}
        self.mixed_column_pages = set()
        self.parallel_pages = set()
        self.parallel_primary = 0
        self._selected_anchors = []
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
                    inferred = False
                    if not match and page_index in self.ocr_pages:
                        match = OCR_DAMAGED_QNO.match(text)
                        inferred = bool(match)
                    if match:
                        number = int(next(group for group in match.groups() if group))
                        if number > 0:
                            self.prefixed.append(Anchor(number, page_index, rect, match.group(0), True, inferred))
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
        self._refresh_columns()

    def _detect_parallel_pages(self):
        """Detect two aligned versions of the same numbered items.

        Repeated, vertically corresponding labels are different evidence from
        newspaper-style columns with increasing numbers. Choose the more
        readable text layer as the primary one and preserve the other original
        language as a reviewable source crop. This is not a translation.
        """
        page_pairs = {}
        for page_index, page in enumerate(self.doc):
            anchors = [a for a in self.prefixed + self.numbered if a.page_index == page_index]
            groups = []
            for column in (0, 1):
                possible = [a for a in anchors if column * .5 <= a.rect.x0 / page.rect.width < column * .5 + .30]
                if not possible:
                    groups = []
                    break
                margin = min(a.rect.x0 / page.rect.width - column * .5 for a in possible)
                groups.append([a for a in possible if abs(a.rect.x0 / page.rect.width - column * .5 - margin) <= .025])
            if len(groups) != 2:
                continue
            pairs = []
            for left in groups[0]:
                matches = [right for right in groups[1] if right.number == left.number]
                if matches:
                    right = min(matches, key=lambda item: abs(item.rect.y0 - left.rect.y0))
                    if abs(right.rect.y0 - left.rect.y0) <= max(50, page.rect.height * .08):
                        pairs.append((left, right))
            unique = {left.number: (left, right) for left, right in pairs}
            if unique:
                page_pairs[page_index] = list(unique.values())
        strong = {index for index, pairs in page_pairs.items()
                  if len(pairs) >= 3 and max(left.rect.y0 for left, _ in pairs) -
                  min(left.rect.y0 for left, _ in pairs) >= 70}
        pages = set(strong)
        # A page with only one or two long questions can inherit the pattern
        # from a neighbouring, strongly evidenced bilingual page.
        pages.update(index for index in page_pairs if any(abs(index - near) == 1 for near in strong))
        for index in range(1, len(self.doc) - 1):
            if index not in page_pairs and index - 1 in strong and index + 1 in strong:
                if not any(a.page_index == index for a in self.prefixed + self.numbered):
                    pages.add(index)
        scores = [0, 0]
        for index in pages:
            width = self.doc[index].rect.width
            for column in (0, 1):
                text = " ".join(value for value, box, _ in self.lines[index]
                                if column * .5 <= (box.x0 + box.x1) / (2 * width) < (column + 1) * .5)
                common = len(re.findall(r"\b(?:the|which|following|with|for|from|are|and|is|of|to|be|this|that|what|one|correct)\b", text, re.I))
                words = sum(len(word) for word in re.findall(r"\b[A-Za-z]{3,}\b", text))
                devanagari = sum("\u0900" <= char <= "\u097f" for char in text)
                broken = sum(text.count(char) for char in "{}$`~\\§")
                scores[column] += common * 8 + words + devanagari - broken * 4
        self.parallel_pages = pages
        self.parallel_primary = 1 if scores[1] > scores[0] else 0

    def _is_primary_anchor(self, anchor):
        if anchor.page_index not in self.parallel_pages:
            return True
        width = self.doc[anchor.page_index].rect.width
        return int(anchor.rect.x0 >= width / 2) == self.parallel_primary

    def _reading_columns(self, page_index):
        if page_index in self.parallel_pages:
            return (self.parallel_primary,)
        return range(self._columns_on(page_index))

    def _refresh_columns(self):
        """Infer one, two or three equal-width columns independently per page.

        Separate, ascending question anchors at matching margins are evidence
        for columns. Multiple long source lines spanning the midpoint, without
        that anchor evidence, are evidence for a full-width page. A page with
        conflicting evidence is still processed conservatively and flagged.
        Ambiguous continuation pages use the document's dominant column count.
        """
        self._detect_parallel_pages()
        signals = {}
        spanning = {}
        for page_index, page in enumerate(self.doc):
            prefixed = [a for a in self.prefixed if a.page_index == page_index]
            numbered = [a for a in self.numbered if a.page_index == page_index]
            candidates = prefixed if prefixed else numbered
            width = page.rect.width
            first_y = min((a.rect.y0 for a in candidates), default=0)
            spanning[page_index] = sum(
                1 for value, box, _ in self.lines[page_index]
                if box.y0 >= first_y - 2 and box.y0 < page.rect.height * .87
                and box.x0 < width * .30 and box.x1 > width * .58
                and not self.edge_counts[self._edge_key(page, value, box)] >= max(2, int(len(self.doc) * .15))
            )
            if page_index in self.parallel_pages:
                signals[page_index] = 2
                continue
            for count in (3, 2):
                groups = [[] for _ in range(count)]
                for anchor in candidates:
                    pos = anchor.rect.x0 / width
                    column = min(count - 1, max(0, int(pos * count)))
                    groups[column].append((pos - column / count, anchor))
                if any(not group for group in groups):
                    continue
                # Cluster each column's outer margin, ignoring more-indented
                # numeric choices which can otherwise look like questions.
                clustered = []
                for group in groups:
                    margin = min(position for position, _ in group)
                    clustered.append([(position, a) for position, a in group if abs(position - margin) <= .025])
                margins = [median(position for position, _ in group) for group in clustered]
                if max(margins) - min(margins) > .055:
                    continue
                if any(max(a.number for _, a in left) >= min(a.number for _, a in right)
                       for left, right in zip(clustered, clustered[1:])):
                    continue
                # Two bare numeric labels can be a question and a side-by-side
                # answer; require extra evidence unless labels are explicit Qs.
                if not prefixed and sum(len(group) for group in clustered) < count + 1:
                    continue
                signals[page_index] = count
                break
        multi = Counter(count for count in signals.values() if count > 1)
        if multi:
            # Keep the historical two-column fallback on a tie, otherwise use
            # the most frequently evidenced layout for continuation pages.
            default = max(multi, key=lambda count: (multi[count], count == 2 and self.two_columns, count))
        else:
            default = 2 if self.two_columns else 1
        self.column_counts = {}
        self.mixed_column_pages = set()
        for page_index in range(len(self.doc)):
            if page_index in signals:
                self.column_counts[page_index] = signals[page_index]
                if spanning[page_index] and page_index not in self.parallel_pages:
                    self.mixed_column_pages.add(page_index)
            elif spanning[page_index] >= 2:
                self.column_counts[page_index] = 1
            else:
                self.column_counts[page_index] = default

    def _columns_on(self, page_index):
        return self.column_counts.get(page_index, 2 if self.two_columns else 1)

    def _column_of(self, anchor):
        count = self._columns_on(anchor.page_index)
        width = self.doc[anchor.page_index].rect.width
        return min(count - 1, max(0, int(anchor.rect.x0 / width * count)))

    def _column_bounds(self, page_index, column):
        count = self._columns_on(page_index)
        width = self.doc[page_index].rect.width
        return width * column / count, width * (column + 1) / count

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
            if not self._is_primary_anchor(anchor):
                continue
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
        self._refresh_columns()

        def col(anchor):
            return self._column_of(anchor)

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
                for column in self._reading_columns(page_index):
                    if page_index == start_page and column < start_col:
                        continue
                    if following and (page_index, column) > (following.page_index, col(following)):
                        break
                    x0, x1 = self._column_bounds(page_index, column)
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
        positioned = sorted(((a.rect.x0 / self.doc[a.page_index].rect.width
                              - col(a) / self._columns_on(a.page_index), a)
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
                    body = line[len(anchor.label):].strip() if line.startswith(anchor.label) else line
                    last = (previous and not following and anchor.number == previous.number + 1
                            and ("?" in line or (len(confirmed) >= 3 and len(body) >= 12)))
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
        prefixed = [anchor for anchor in self.prefixed if self._is_primary_anchor(anchor)]
        plain = [anchor for anchor in self.numbered if self._is_primary_anchor(anchor)]
        if not prefixed:
            return self._numbered_anchors()
        leaders = {}
        for anchor in prefixed:
            page = self.doc[anchor.page_index]
            column = self._column_of(anchor)
            offset = anchor.rect.x0 / page.rect.width - column / self._columns_on(anchor.page_index)
            leaders.setdefault(column, []).append(offset)
        all_leaders = [value for values in leaders.values() for value in values]
        leaders = {column: median(values) for column, values in leaders.items()}

        def column_of(anchor):
            return self._column_of(anchor)

        def reading_order(anchor):
            return anchor.page_index, column_of(anchor), anchor.rect.y0

        if prefixed and len(plain) >= max(6, len(prefixed) * 2):
            # A few Q1/Q2 references, section headings or formula labels must
            # not override a long, coherent plain-numbered paper. Require a
            # strong sequence rather than merely counting all digit labels,
            # since numeric answer choices can also look like question labels.
            numbered = self._numbered_anchors()
            longest = run = 0
            previous_number = None
            for anchor in numbered:
                run = run + 1 if anchor.number == (previous_number or 0) + 1 else 1
                longest = max(longest, run)
                previous_number = anchor.number
            if (len(numbered) >= max(6, len(prefixed) * 2)
                    and longest >= 6):
                numbers = {anchor.number for anchor in numbered}
                combined = list(numbered)
                ignored = 0
                for candidate in sorted(prefixed, key=reading_order):
                    if candidate.number in numbers:
                        ignored += 1
                        continue
                    before = [item for item in combined if reading_order(item) < reading_order(candidate)]
                    after = [item for item in combined if reading_order(item) > reading_order(candidate)]
                    previous = max(before, key=reading_order) if before else None
                    following = min(after, key=reading_order) if after else None
                    sequential = ((previous and candidate.number == previous.number + 1)
                                  or (following and candidate.number + 1 == following.number))
                    neighbour = previous or following
                    margin_ok = False
                    if neighbour:
                        def relative_margin(item):
                            page_width = max(self.doc[item.page_index].rect.width, 1)
                            count = self._columns_on(item.page_index)
                            return item.rect.x0 / page_width - self._column_of(item) / count
                        margin_ok = abs(relative_margin(candidate) - relative_margin(neighbour)) < 0.04
                    if sequential and margin_ok:
                        combined.append(candidate)
                        numbers.add(candidate.number)
                    else:
                        ignored += 1
                if ignored:
                    self.log("Warning: Some isolated Q labels were treated as references rather than "
                             "questions; review the question count and the original paper.")
                return sorted(combined, key=reading_order)

        aligned = []
        for anchor in plain:
            page = self.doc[anchor.page_index]
            column = column_of(anchor)
            expected = leaders.get(column, median(all_leaders) if all_leaders else None)
            offset = anchor.rect.x0 / page.rect.width - column / self._columns_on(anchor.page_index)
            if expected is not None and abs(offset - expected) <= 0.025:
                aligned.append(anchor)

        # Numeric answer labels may align with an explicit Q label. Treat an
        # adjacent 1,2,... run following a question as choices, not new
        # question anchors. The prefixed anchors also bound the run.
        ordered = sorted(prefixed + aligned, key=reading_order)
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
                x0, x1 = self._column_bounds(candidate.page_index, column)
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

        first_prefixed = min(prefixed, key=reading_order)
        anchors = list(prefixed)
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
            x0, x1 = self._column_bounds(anchor.page_index, column)
            following = [
                other.rect.y0 for other in aligned + prefixed
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
        columns = self._columns_on(page_index)
        for text, rect, spans in self.lines[page_index]:
            # Repeated short choice labels at the very top/bottom of many
            # pages are content, not headers or footers. Cropping them can
            # silently remove a question stem or its first/last answer.
            repeated = (self.edge_counts[self._edge_key(page, text, rect)] >= threshold
                        and not OPTION_AT_START.match(text))
            if repeated and rect.y0 < page.rect.height * 0.09:
                top = max(top, rect.y1 + 1)
            elif repeated and rect.y0 > page.rect.height * 0.87:
                bottom = min(bottom, rect.y0 - 1)
            # A large title spanning both columns is not a continuation of
            # the previous question when the right column is read.
            elif (
                columns > 1 and any(rect.x0 < page.rect.width * boundary / columns < rect.x1
                                    for boundary in range(1, columns)) and rect.y1 < first_y
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
        sizes = sorted(span["size"] for span in spans)
        # A run of fractions can contain more small numerator/denominator
        # spans than body spans. A median then mistakes the small glyphs for
        # normal text and silently misses the visual formatting. Use the upper
        # quartile as a conservative body-size reference instead.
        typical_size = sizes[min(len(sizes) - 1, len(sizes) * 3 // 4)] if sizes else 0
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

    def _explicit_essay_instructions(self):
        # Only a strong paper-wide instruction is used; a mention of an essay
        # in an ordinary multiple-choice statement is not enough evidence.
        pattern = re.compile(r"\bessay\s+type\s+question\b.{0,100}\bshort\s+note\b.{0,100}\b(?:compulsory|required)\b", re.I)
        for index in range(min(2, len(self.doc))):
            try:
                if index in self.ocr_pages:
                    page, textpage = self.ocr_pages[index]
                    text = page.get_text("text", textpage=textpage)
                else:
                    text = self.doc[index].get_text("text")
                if pattern.search(_compact(text)):
                    return True
            except Exception:
                continue
        return False

    def _declared_question_counts(self):
        """Find explicit paper-wide totals on opening pages, never section totals."""
        patterns = (
            re.compile(r"\btotal\s+(?:number\s+of\s+)?questions?\s*[:=–—-]?\s*(\d{1,4})\b", re.I),
            re.compile(r"\b(?:question\s+paper|test\s+booklet|entire\s+paper|examination)\b"
                       r"[^.]{0,120}?\b(?:contains|has|comprises|consists\s+of)\s+(\d{1,4})\s+"
                       r"(?:multiple[-\s]+choice\s+)?questions?\b", re.I),
        )
        counts = set()
        for index in range(min(2, len(self.doc))):
            try:
                if index in self.ocr_pages:
                    page, textpage = self.ocr_pages[index]
                    text = page.get_text("text", textpage=textpage)
                else:
                    text = self.doc[index].get_text("text")
                text = _compact(text)
                counts.update(int(match.group(1)) for pattern in patterns for match in pattern.finditer(text)
                              if 0 < int(match.group(1)) <= 2000)
            except Exception:
                continue
        return counts

    def extract(self):
        anchors = self._anchors()
        if not anchors:
            return None
        self._selected_anchors = anchors
        if self.parallel_pages:
            self.log("Parallel numbered columns detected: one text layer was used and the other "
                     "is preserved as source images when boundaries are reliable. Review both languages and the question count.")
        by_page = {}
        for anchor in anchors:
            by_page.setdefault(anchor.page_index, []).append(anchor)
        modes = {self._columns_on(index) for index in range(len(self.doc))}
        description = ({1: "single column", 2: "two columns", 3: "three columns"}.get(next(iter(modes)))
                       if len(modes) == 1 else "mixed page columns")
        self.log(
            f"{'OCR-assisted' if self.ocr_pages else 'Digital'} layout detected: {description}; "
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
            for column in self._reading_columns(page_index):
                x0, x1 = self._column_bounds(page_index, column)
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

        multi_section = False
        essay_instructions = self._explicit_essay_instructions()
        if essay_instructions:
            self.log("Warning: The paper specifies essay and short-note answers. Single-choice CBT grading does not support these questions.")
        for question in questions:
            self._finalize(question)
            source_text = " ".join(segment.text for segment in question.segments)
            essay_parts = (len(question.options) < 3 or
                           (re.search(r"\bmarks\s*[:.]\s*\d+", source_text, re.I) and
                            re.search(r"^\s*OR\s*$", source_text, re.I | re.M)))
            if essay_instructions and essay_parts:
                # Subparts (a)/(b) and OR alternatives are not MCQ answers.
                # Preserve the source text as-is, remove the misleading parsed
                # options, and leave any option-assigned figures unassigned.
                label = re.compile(r"(?<!\w)" + re.escape(question.label.strip()) + r"(?!\d)")
                question.text = _compact(label.sub("", source_text, count=1))
                question.options = []
                question.tables = []
                for figure in question.extra_figures:
                    if figure.get("option_key"):
                        figure["option_key"] = None
                question.issues.insert(0, "Printed essay/short-note questions cannot be graded as single-choice answers; review or exclude this item.")
            # The application scores one selected option only. Explicit
            # multi-answer instructions apply until a later explicit
            # single-answer instruction; never infer or verify an answer key.
            multi = MULTIPLE_CORRECT.search(source_text)
            single = SINGLE_CORRECT.search(source_text)
            if single and (not multi or single.start() > multi.start()):
                multi_section = False
            elif multi:
                multi_section = True
            if multi_section:
                question.issues.append("Printed instructions may allow multiple correct choices; this test supports single-choice grading only. Review or exclude this question.")
            if len(question.options) < 2:
                question.issues.append("A complete supported answer-choice list was not found; image-only, numerical or incomplete questions need manual review.")
            elif len(question.options) < 5:
                expected = {option["key"] for option in question.options}
                possible = {_normalize_option(_option_raw(match))
                            for option in question.options for match in OPTION_MARKER.finditer(option.get("text", ""))}
                if any(key not in expected and key > max(expected) for key in possible):
                    question.issues.append("Additional option labels may be embedded in an extracted choice; verify the choice boundaries.")
        declared = self._declared_question_counts()
        if len(declared) == 1:
            expected = next(iter(declared))
            if expected != len(questions):
                self.log(f"Warning: The paper states {expected} questions, but {len(questions)} were detected. "
                         "Check for missing or duplicated items; do not assume the printed total is correct.")
        elif len(declared) > 1:
            self.log("Warning: Conflicting paper-wide question totals were found; verify the question count manually.")
        if questions and questions[0].number > 1:
            self.log(f"Warning: The first detected question is {questions[0].number}; "
                     "earlier questions may be missing or intentionally omitted.")
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
                        [] if segment.page_index in self.ocr_pages else _find_geometric_tables(page).tables[:100]
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
                        text_tables = _find_geometric_tables(page, clip=clip, strategy="text").tables[:20]
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
                question.table_regions.append((segment.page_index, fitz.Rect(box), fitz.Rect(segment.rect)))
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

    def _parallel_source_regions(self, question):
        regions = []
        for segment in question.segments:
            index = segment.page_index
            if index not in self.parallel_pages:
                continue
            primary = sorted((a for a in self._selected_anchors if a.page_index == index),
                             key=lambda a: a.rect.y0)
            alternate = [a for a in self.prefixed + self.numbered
                         if a.page_index == index and not self._is_primary_anchor(a)]
            top, bottom = self._content_bounds(index, primary)
            own = next((a for a in primary if a.number == question.number
                        and segment.rect.y0 <= a.rect.y0 < segment.rect.y1), None)
            following = next((a for a in primary if a.rect.y0 >= segment.rect.y1 - 1), None)
            def counterpart(anchor):
                if not anchor:
                    return None
                options = [a for a in alternate if a.number == anchor.number]
                if not options:
                    return None
                result = min(options, key=lambda a: abs(a.rect.y0 - anchor.rect.y0))
                limit = max(50, self.doc[index].rect.height * .08)
                return result if abs(result.rect.y0 - anchor.rect.y0) <= limit else None
            other = counterpart(own)
            next_other = counterpart(following)
            if (not own and not following) or (own and not other) or (following and not next_other):
                question.issues.append("The parallel-language boundary could not be matched; inspect the original page.")
                continue
            y0 = max(top, other.rect.y0 - 5) if other else top
            y1 = min(bottom, next_other.rect.y0 - 5) if next_other else bottom
            if y1 <= y0 or (not own and segment.rect.y0 > top + 2):
                question.issues.append("The parallel-language boundary could not be matched; inspect the original page.")
                continue
            x0, x1 = self._column_bounds(index, 1 - self.parallel_primary)
            region = fitz.Rect(x0, y0, x1, y1)
            if region.height < 22 and not self._text_in_rect(index, region).strip():
                continue
            regions.append((index, region))
        return regions

    def _save_source_snapshot(self, question, page_index, rect, source):
        """Keep a bounded, original-PDF crop; never draw or infer content."""
        page = self.doc[page_index]
        rect = fitz.Rect(rect) & page.rect
        if rect.is_empty or rect.width < 10 or rect.height < 8:
            return False
        if any(rect.intersects(line) and ANSWER_OR_SOLUTION.search(value)
               for value, line, _ in self.lines[page_index]):
            message = "A source image was omitted because a printed answer or solution may be visible; review the original."
            if message not in question.issues:
                question.issues.append(message)
            return False
        try:
            # The limit applies to the rendered side, including on unusually
            # large physical pages. Original coordinates remain available.
            factor = min(2.0, 2200 / max(rect.width, rect.height))
            pix = page.get_pixmap(matrix=fitz.Matrix(factor, factor), clip=rect, alpha=False)
            question.extra_figures.append({
                "page": page_index + 1, "box": source_box(page, rect),
                "data": pix.tobytes("png"), "option_key": "", "source": source,
            })
            return True
        except Exception:
            question.issues.append("Could not save an original source image; compare the question with its PDF page.")
            return False

    def _has_positioned_fraction(self, segment, markers):
        """Recognize source geometry of a short bar with glyphs above/below.

        This is only a reason to preserve an image, never a transcription or an
        answer. It also catches fractions whose glyph sizes are identical to
        body text and would therefore evade the font-size heuristic.
        """
        if not markers or segment.page_index in self.ocr_pages:
            return False
        drawings = self._drawings_by_page.get(segment.page_index, [])
        low = min(box.y0 for _, box in markers) - 15
        high = max(box.y1 for _, box in markers) + 35
        spans = [span for _, line, parts in self.lines[segment.page_index]
                 if segment.rect.intersects(line) and low - 15 <= line.y0 <= high + 15
                 for span in parts if span.get("text", "").strip()]
        for drawing in drawings:
            bar = fitz.Rect(drawing.get("rect", (0, 0, 0, 0)))
            if not (4 <= bar.width <= 80 and bar.height <= 1.5 and low <= bar.y0 <= high
                    and segment.rect.x0 <= bar.x0 < bar.x1 <= segment.rect.x1):
                continue
            above, below = False, False
            for span in spans:
                text = span.get("text", "").strip()
                if len(text) > 8 or not re.search(r"\w", text):
                    continue
                box = fitz.Rect(span["bbox"])
                center = (box.tl + box.br) / 2
                if not bar.x0 - 3 <= center.x <= bar.x1 + 3:
                    continue
                above = above or -16 <= center.y - bar.y0 < -0.5
                below = below or 0.5 < center.y - bar.y0 <= 16
            if above and below:
                return True
        return False

    def _bounded_incomplete_regions(self, question):
        """Yield conservative source regions when no choice list was recovered.

        A next question in the same column provides an end boundary. At a
        page break, require a nearby sequential anchor at the start of the next
        page and a short final region. Never infer options from these images.
        """
        for segment in question.segments:
            index, box = segment.page_index, segment.rect
            page = self.doc[index]
            same_column = [a for a in self._selected_anchors if a.page_index == index
                           and box.x0 <= a.rect.x0 < box.x1]
            starts_here = any(a.number == question.number and abs(a.rect.y0 - box.y0 - 5) <= 1.5
                              for a in same_column)
            if not starts_here:
                continue  # Do not save an unbounded continuation or a cover.
            if any(abs(a.rect.y0 - box.y1 - 5) <= 1.5 for a in same_column):
                yield index, box
                continue
            if index + 1 >= len(self.doc) or box.height > page.rect.height * .65:
                continue
            _, bottom = self._content_bounds(index, [a for a in self._selected_anchors if a.page_index == index])
            if abs(box.y1 - bottom) > 1.5:
                continue
            next_page = [a for a in self._selected_anchors if a.page_index == index + 1]
            if not next_page:
                continue
            next_width = self.doc[index + 1].rect.width
            first = min(next_page, key=lambda a: (int(a.rect.x0 / next_width * self._columns_on(index + 1)), a.rect.y0))
            if first.number == question.number + 1 and first.rect.y0 <= self.doc[index + 1].rect.height * .25:
                yield index, box

    def _preserve_source_snapshots(self, question, markers, first_option_segment):
        """Supplement uncertain maths/scans and tables with source pixels.

        These group images are not assigned to individual choices and do not
        replace any parsed text. The display explains that source labels are in
        their original order; users must review both crop and transcription.
        """
        all_markers = [(key, box) for index in range(len(question.segments))
                       for key, box in markers.get(index, [])]
        complete = (len(question.options) >= 2 and len(all_markers) == len(question.options)
                    and [key for key, _ in all_markers] == [o["key"] for o in question.options])
        fraction_segments = {index for index, segment in enumerate(question.segments)
                             if self._has_positioned_fraction(segment, markers.get(index, []))}
        if fraction_segments and not any("Mathematical formatting" in issue for issue in question.issues):
            question.issues.append("Positioned fraction-like content may be inaccurate; compare with the source.")
        saved_options = False
        if complete and first_option_segment is not None:
            for index in range(first_option_segment, len(question.segments)):
                segment = question.segments[index]
                choice_markers = markers.get(index, [])
                # OCR and scan text can be wrong even when font metrics look
                # ordinary; digital pages need a clear complex-math signal.
                if not choice_markers or not (segment.has_complex_math or index in fraction_segments
                                              or segment.page_index in self.ocr_pages
                                              or segment.page_index in self.raster_pages):
                    continue
                box = fitz.Rect(segment.rect)
                box.y0 = max(box.y0, min(marker.y0 for _, marker in choice_markers) - 12)
                # Do not capture the rest of a page: it may contain unseen
                # questions or an answer key if segmentation failed. This is a
                # supplemental view, not a promise to include a tall diagram.
                box.y1 = min(box.y1, max(marker.y1 for _, marker in choice_markers) + 35)
                if self._save_source_snapshot(question, segment.page_index, box, "option-source"):
                    saved_options = True
        if saved_options:
            question.issues.append("Original answer choices saved as a source image; check its boundaries, order and extracted text.")

        saved_stem = False
        # A referenced but unstructured table is already preserved as a
        # statement-region table snapshot below; avoid saving identical crops.
        needs_table_fallback = not question.table_regions and TABLE_REFERENCE.search(question.text)
        if complete and first_option_segment is not None and not needs_table_fallback:
            for index, segment in enumerate(question.segments):
                if index > first_option_segment:
                    break
                if not (segment.has_complex_math or index in fraction_segments
                        or segment.page_index in self.ocr_pages or segment.page_index in self.raster_pages):
                    continue
                box = fitz.Rect(segment.rect)
                if index == first_option_segment:
                    option_boxes = [marker for _, marker in markers.get(index, [])]
                    if not option_boxes:
                        continue
                    box.y1 = min(box.y1, min(marker.y0 for marker in option_boxes) - 3)
                if box.height >= 20 and self._save_source_snapshot(question, segment.page_index, box, "stem-source"):
                    saved_stem = True
        if saved_stem:
            question.issues.append("Original question region saved as a source image; check formulas and extracted text.")

        if len(question.options) < 2:
            # A diagram-only or otherwise incomplete choice list cannot be
            # made gradable safely. Preserve the bounded original pixels to
            # help the reviewer reconstruct it, without inventing any labels.
            saved_incomplete = False
            for page_index, box in self._bounded_incomplete_regions(question):
                saved_incomplete |= self._save_source_snapshot(question, page_index, box, "stem-source")
            if saved_incomplete:
                question.issues.append("Original question and possible choices saved as a source image; manually repair and verify this incomplete item.")

        saved_table = False
        for page_index, box, segment_box in question.table_regions:
            crop = (box + (-3, -3, 3, 3)) & segment_box
            if self._save_source_snapshot(question, page_index, crop, "table-source"):
                saved_table = True
        if needs_table_fallback and complete and first_option_segment is not None:
            # For OCR, a damaged grid, or a rejected table structure, preserve
            # the statement region above the confirmed first option. No cells
            # or table semantics are invented from this screenshot.
            for index, segment in enumerate(question.segments):
                if first_option_segment is not None and index > first_option_segment:
                    break
                crop = fitz.Rect(segment.rect)
                if index == first_option_segment:
                    option_boxes = [box for _, box in markers.get(index, [])]
                    if not option_boxes:
                        continue
                    crop.y1 = min(crop.y1, min(box.y0 for box in option_boxes) - 3)
                if crop.height >= 30 and self._save_source_snapshot(question, segment.page_index, crop, "table-source"):
                    saved_table = True
        if saved_table:
            question.issues.append("Original table or statement region saved as a source image; verify it and all extracted cells.")
        parallel_regions = self._parallel_source_regions(question)
        if parallel_regions:
            question.issues.append("Parallel-language columns were separated; check the other original region and choice order before use.")
            for page_index, box in parallel_regions:
                question.source_regions.append({"page": page_index + 1, "box": source_box(self.doc[page_index], box)})
                self._save_source_snapshot(question, page_index, box, "parallel-source")
        ranks = {"stem-source": 0, "table-source": 1, "parallel-source": 2, "option-source": 3}
        regular = [figure for figure in question.extra_figures if figure["source"] not in ranks]
        snapshots = [figure for figure in question.extra_figures if figure["source"] in ranks]
        snapshots.sort(key=lambda figure: (ranks[figure["source"]], figure["page"]))
        question.extra_figures = regular + snapshots

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
        grid_markers = None
        if first_option_offset is not None and [o["key"] for o in question.options] == ["A", "B"]:
            offset = 0
            first_index = None
            for index, segment_text in enumerate(texts):
                if first_option_offset < offset + len(segment_text) + 1:
                    first_index = index
                    break
                offset += len(segment_text) + 1
            if first_index is not None:
                segment = question.segments[first_index]
                initial = self._option_label_boxes(question, text, first_option_offset)
                ocr = self.ocr_pages.get(segment.page_index)
                page, textpage = ocr if ocr else (self.doc[segment.page_index], None)
                recovered = recover_column_major_grid(page, segment, initial[first_index], question.options,
                                                       OPTION_MARKER, _normalize_option, _option_style, textpage)
                if recovered:
                    question.options, grid = recovered
                    grid_markers = {index: [] for index in range(len(question.segments))}
                    grid_markers[first_index] = grid
                    question.issues.append("Column-major option layout was reordered from source geometry; verify every choice.")
        # Only geometrically unambiguous ruled fractions are converted to
        # LaTeX. A human still needs to verify every complex-math question.
        reconstructed_by_segment = {}
        for segment_index, segment in enumerate(question.segments):
            if grid_markers or segment.page_index in self.ocr_pages or not segment.has_complex_math or not question.options:
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
        if any(ANSWER_OR_SOLUTION.search(segment.text) for segment in question.segments):
            question.issues.append("A printed answer or solution may be included in this question; review before using it in a test.")
        if any(segment.page_index in self.mixed_column_pages for segment in question.segments):
            question.issues.append("Text spans a detected column boundary; this page may contain a mixed layout and needs source review.")
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
        markers = grid_markers or self._option_label_boxes(question, text, first_option_offset)
        # Flat reading order can interleave wrapped answers printed side by
        # side. Repair only complete, non-mathematical runs contained in one
        # segment, and only when every source token remains accounted for.
        if first_option_segment is not None and question.options and not grid_markers:
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
        self._preserve_source_snapshots(question, markers, first_option_segment)
