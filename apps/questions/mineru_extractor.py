"""Optional local MinerU extraction backend.

MinerU is run **as a subprocess** using a separate virtualenv
(``settings.MINERU_PYTHON``) so its heavy dependencies never enter this app.
The subprocess (``mineru_runner.py``) parses the PDF entirely locally — no cloud,
no remote service, telemetry disabled — and emits a ``content_list.json`` plus
cropped figure images. This module converts that into the same
``ExtractedQuestion`` / ``ExtractedFigure`` staging records the built-in pipeline
produces, so the review, flagging, privacy and approval workflow is unchanged.

MinerU output is best-effort OCR/ML: fractions, figures and tables are far better
than the plain pipeline, but transcription slips happen, so every item is flagged
for human review and nothing is auto-confirmed.
"""
import json
import os
import re
import subprocess
import tempfile
from decimal import Decimal
from html.parser import HTMLParser

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import transaction
import pymupdf as fitz

from .extractor import ReviewWorkPreserved
from .models import ExtractedFigure, ExtractedQuestion, QuestionPaperUpload
from .question_types import classify_question

_PH_OPEN, _PH_CLOSE = "", ""
_PLACEHOLDER = re.compile(_PH_OPEN + r"(\d+)" + _PH_CLOSE)
_OPTION_MARKER = re.compile(r"\((\d)\)")
_Q_START = re.compile(r"(?m)^\s*(\d{1,3})\s*[.．]\s")


def mineru_is_configured():
    """True when a MinerU interpreter is configured and present."""
    path = getattr(settings, "MINERU_PYTHON", "")
    return bool(path) and os.path.isfile(path)


def clean_markup(text):
    """Strip MinerU Markdown/HTML emphasis, keeping LaTeX ($...$) and text."""
    text = re.sub(r"<sup>(.*?)</sup>", r"^{\1}", text, flags=re.S)
    text = re.sub(r"<sub>(.*?)</sub>", r"_{\1}", text, flags=re.S)
    text = re.sub(r"</?(?:strong|b|em|i|u|mark)>", "", text)
    text = text.replace("**", "").replace("__", "")
    return text


def _mask_math(text):
    """Replace $...$/$$...$$ spans with equal-length filler so markers inside math
    (e.g. a ``(2)`` in a formula) are never mistaken for option labels."""
    return re.sub(r"\$\$.*?\$\$|\$[^$]*\$", lambda m: "\x00" * len(m.group(0)), text, flags=re.S)


def split_options(chunk):
    """Return (stem, options, first_option_offset).

    ``options`` is a list of {"key","text","start","end"} with char ranges in the
    original chunk. Uses the last complete ``(1),(2),…`` run (math-aware), mirroring
    the built-in parser so a ``(2)`` referenced inside the stem is not a label.
    """
    masked = _mask_math(chunk)
    markers = [(m.start(), m.end(), int(m.group(1))) for m in _OPTION_MARKER.finditer(masked)]
    best = []
    for index, (start, _end, num) in enumerate(markers):
        if num != 1:
            continue
        run = [markers[index]]
        expected = 2
        for later in markers[index + 1:]:
            if later[2] == expected:
                run.append(later)
                expected += 1
                if expected > 5:
                    break
            elif later[2] == 1:
                break
        if len(run) >= 2:
            best = run  # last complete run wins
    if not best:
        return chunk.strip(), [], None
    first_offset = best[0][0]
    options = []
    for index, (start, end, num) in enumerate(best):
        text_end = best[index + 1][0] if index + 1 < len(best) else len(chunk)
        options.append({"key": chr(ord("A") + num - 1),
                        "text": chunk[end:text_end], "start": end, "end": text_end})
    return chunk[:first_offset].strip(), options, first_offset


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self._row, self._cell, self._text = [], None, None, []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = {"header": tag == "th",
                          "colspan": _to_int(a.get("colspan"), 1),
                          "rowspan": _to_int(a.get("rowspan"), 1)}
            self._text = []

    def handle_data(self, data):
        if self._cell is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._cell["text"] = re.sub(r"\s+", " ", "".join(self._text)).strip()
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


def _to_int(value, default):
    try:
        number = int(value)
        return number if number >= 1 else default
    except (TypeError, ValueError):
        return default


def html_table_to_structured(html):
    """Convert a simple MinerU HTML <table> to the app's structured schema, or None."""
    parser = _TableParser()
    try:
        parser.feed(html or "")
    except Exception:
        return None
    rows = [row for row in parser.rows if row]
    if not rows:
        return None
    columns = max(sum(cell["colspan"] for cell in row) for row in rows)
    if not 1 <= columns <= 20 or len(rows) > 100:
        return None
    # Reject ragged tables (holes) rather than silently reshaping the source.
    for row in rows:
        if sum(cell["colspan"] for cell in row) != columns:
            return None
    clamped = []
    for row in rows:
        clamped.append([
            {"text": cell["text"][:4000],
             "colspan": min(cell["colspan"], columns),
             "rowspan": min(cell["rowspan"], len(rows)),
             "header": cell["header"]}
            for cell in row
        ])
    return {"columns": columns, "rows": clamped}


def parse_mineru_content(content, image_dir):
    """Convert a MinerU content list into a list of question dicts.

    Each question: {number, text, options, tables, figures, issues}. Figures carry
    an absolute source path and an optional option_key; the first stem figure is
    the primary image.
    """
    attachments = []
    parts = []
    for item in content:
        kind = item.get("type")
        if kind == "text":
            raw = item.get("text") or ""
            # Drop underlined section/syllabus banners (no options, no question
            # number) so they don't pollute the preceding question's statement.
            if "<u>" in raw and not re.search(r"\(\d\)", raw) and not re.match(r"\s*(?:\*\*)?\s*\d{1,3}\s*[.．]", raw):
                continue
            cleaned = clean_markup(raw)
            if cleaned.strip():
                parts.append(cleaned)
        elif kind == "equation":
            latex = (item.get("text") or "").strip()
            if latex:
                parts.append(latex if latex.startswith("$") else f"${latex}$")
        elif kind in ("image", "figure"):
            path = item.get("img_path") or ""
            if path:
                parts.append(f"{_PH_OPEN}{len(attachments)}{_PH_CLOSE}")
                attachments.append({"kind": "image", "path": os.path.join(image_dir, path),
                                    "page": (item.get("page_idx") or 0) + 1})
        elif kind == "table":
            html = item.get("table_body") or item.get("html") or ""
            if html.strip():
                parts.append(f"{_PH_OPEN}{len(attachments)}{_PH_CLOSE}")
                attachments.append({"kind": "table", "html": html,
                                    "page": (item.get("page_idx") or 0) + 1})
    joined = "\n".join(parts)

    starts = []
    last = 0
    for match in _Q_START.finditer(joined):
        number = int(match.group(1))
        if number > last and number - last <= 4:  # increasing, bounded gap (tolerates a missed Q)
            starts.append((match.start(), match.end(), number))
            last = number
    if not starts:
        return []

    questions = []
    for index, (start, body_start, number) in enumerate(starts):
        end = starts[index + 1][0] if index + 1 < len(starts) else len(joined)
        chunk = joined[body_start:end]
        stem, options, first_offset = split_options(chunk)
        tables, figures = [], []
        issues = []

        def region_of(offset):
            if first_offset is None or offset < first_offset:
                return "stem"
            for option in options:
                if option["start"] <= offset < option["end"]:
                    return option["key"]
            return "stem"

        for ph in _PLACEHOLDER.finditer(chunk):
            attachment = attachments[int(ph.group(1))]
            target = region_of(ph.start())
            if attachment["kind"] == "image":
                figures.append({"path": attachment["path"], "page": attachment["page"],
                                "option_key": None if target == "stem" else target})
            else:
                structured = html_table_to_structured(attachment["html"])
                if structured and target == "stem":
                    tables.append(structured)
                    stem = f"{stem}\n[[table:{len(tables)}]]"
                else:
                    issues.append("A detected table could not be structured; check the source.")

        strip = lambda value: _PLACEHOLDER.sub("", value).strip()
        stem = strip(stem)
        clean_options = [{"key": option["key"], "text": strip(option["text"])} for option in options]
        questions.append({"number": number, "text": stem, "options": clean_options,
                          "tables": tables, "figures": figures, "issues": issues})
    return questions


class MinerUQuestionExtractor:
    """Run MinerU locally (subprocess) and stage its output for review."""

    def __init__(self, paper_upload: QuestionPaperUpload):
        self.paper = paper_upload
        self.logs = []
        self._private_files_written = []

    def _log(self, message):
        self.logs.append(message)

    def extract(self) -> int:
        previous_status = self.paper.status
        self.paper.status = QuestionPaperUpload.ProcessingStatus.PROCESSING
        self.paper.save(update_fields=["status"])
        self._log(f"Starting local MinerU extraction for paper: '{self.paper.title}'")
        saved = 0
        tmpdir = None
        try:
            if not mineru_is_configured():
                raise RuntimeError("MinerU is not configured on this server (MINERU_PYTHON is unset or missing).")
            source = self.paper.private_pdf_file if self.paper.is_student_private else self.paper.pdf_file
            source.open("rb")
            try:
                pdf_bytes = source.read()
            finally:
                source.close()
            with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
                page_count = len(doc)
            self.paper.total_pages = page_count
            if page_count > settings.MINERU_MAX_PAGES:
                raise ValueError(f"The PDF has {page_count} pages; the MinerU limit is {settings.MINERU_MAX_PAGES}.")

            tmpdir = tempfile.mkdtemp(prefix="mineru_")
            pdf_path = os.path.join(tmpdir, "input.pdf")
            out_dir = os.path.join(tmpdir, "out")
            with open(pdf_path, "wb") as handle:
                handle.write(pdf_bytes)

            content = self._run_mineru(pdf_path, out_dir)
            items = parse_mineru_content(content, out_dir)
            if not items:
                # Never overwrite prior review work with an empty result.
                self.paper.status = QuestionPaperUpload.ProcessingStatus.FAILED
                self._log("MinerU produced no recognizable questions. Review the PDF and the log.")
            else:
                with transaction.atomic():
                    locked = QuestionPaperUpload.objects.select_for_update().get(pk=self.paper.pk)
                    if locked.extracted_questions.exists() or locked.figures.exists():
                        raise ReviewWorkPreserved("Existing review work was preserved; upload a new copy to re-extract.")
                    for item in items:
                        self._save_item(item)
                        saved += 1
                self.paper.questions_found = saved
                self.paper.status = QuestionPaperUpload.ProcessingStatus.EXTRACTED
                self._log(f"MinerU extraction completed: {saved} questions staged. Review every question, formula, option, table and figure.")
        except ReviewWorkPreserved as exc:
            saved = 0
            self._log(f"Warning: {exc}")
            self.paper.status = previous_status
        except Exception as exc:
            saved = 0
            if self.paper.is_student_private:
                from .private_storage import private_practice_storage
                for name in self._private_files_written:
                    private_practice_storage.delete(name)
            self._log(f"MinerU extraction error: {exc}")
            self.paper.status = QuestionPaperUpload.ProcessingStatus.FAILED
        finally:
            if tmpdir:
                import shutil
                shutil.rmtree(tmpdir, ignore_errors=True)

        self.paper.extraction_log = "\n".join(self.logs)
        self.paper.save()
        return saved

    def _run_mineru(self, pdf_path, out_dir):
        runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mineru_runner.py")
        env = dict(os.environ)
        env["MINERU_TELEMETRY_DISABLED"] = "1"
        env["MINERU_MODEL_SOURCE"] = settings.MINERU_MODEL_SOURCE
        effort = settings.MINERU_EFFORT if settings.MINERU_EFFORT in ("flash", "medium") else "medium"
        command = [settings.MINERU_PYTHON, runner, "--pdf", pdf_path, "--out", out_dir,
                   "--effort", effort, "--model-source", settings.MINERU_MODEL_SOURCE]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=settings.MINERU_TIMEOUT_SECONDS, env=env, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"MinerU timed out after {settings.MINERU_TIMEOUT_SECONDS}s.")
        if result.returncode != 0:
            tail = (result.stderr or result.stdout or "").strip()[-500:]
            raise RuntimeError(f"MinerU subprocess failed (exit {result.returncode}). {tail}")
        content_path = os.path.join(out_dir, "content_list.json")
        if not os.path.isfile(content_path):
            raise RuntimeError("MinerU did not produce a content list.")
        with open(content_path, encoding="utf-8") as handle:
            return json.load(handle)

    def _save_item(self, item):
        text = (item.get("text") or "").strip()
        options = item.get("options") or []
        issues = list(item.get("issues") or [])
        issues.insert(0, "Extracted by local MinerU (OCR/ML); verify text, formulas, options, tables and figures against the source.")
        if not text:
            issues.append("Missing question statement.")
        detected_type, type_reason = classify_question(text, options)
        if detected_type in (ExtractedQuestion.DetectedType.NUMERICAL, ExtractedQuestion.DetectedType.SUBJECTIVE):
            issues.append(type_reason)
        else:
            if len(options) < 2:
                issues.append(f"Only {len(options)} option(s) detected. Minimum 2 required for MCQ.")
            elif any(not option.get("text", "").strip() for option in options):
                issues.append("One or more options are empty; check the source.")
            if detected_type == ExtractedQuestion.DetectedType.MULTIPLE:
                issues.append(type_reason)
        if len({option.get("key") for option in options}) != len(options):
            issues.append("Duplicate option labels detected; compare with the source.")

        figures = item.get("figures") or []
        stem_figures = [figure for figure in figures if figure.get("option_key") is None]
        primary = stem_figures[0] if stem_figures else None

        extracted = ExtractedQuestion(
            paper=self.paper,
            question_number=item.get("number", 1),
            question_text=text,
            options=options,
            tables=item.get("tables") or [],
            detected_type=detected_type,
            page_number=(primary or (figures[0] if figures else {})).get("page", 1) if figures else 1,
            source_pages=sorted({figure.get("page", 1) for figure in figures}) or [1],
            extraction_issues=issues,
            status=ExtractedQuestion.Status.EXTRACTED,
            is_flagged=True,  # all MinerU output needs review
            flag_reason=" ".join(dict.fromkeys(issues))[:255],
            positive_marks=Decimal("4.00"),
            negative_marks=Decimal("1.00"),
        )
        if primary:
            data = self._read_figure(primary["path"])
            if data:
                filename = f"paper_{self.paper.pk}_q{item.get('number')}_mineru.png"
                target = extracted.private_image if self.paper.is_student_private else extracted.image
                target.save(filename, ContentFile(data), save=False)
                if self.paper.is_student_private:
                    self._private_files_written.append(target.name)
        extracted.save()

        order = 0
        for figure in figures:
            if figure is primary:
                continue
            data = self._read_figure(figure["path"])
            if not data:
                continue
            record = ExtractedFigure(
                paper=self.paper, question=extracted,
                option_key=figure.get("option_key") or "",
                page_number=figure.get("page", 1), bounding_box=[], source="mineru", order=order)
            target = record.private_image if self.paper.is_student_private else record.image
            target.save(f"paper_{self.paper.pk}_q{item.get('number')}_mineru_{order}.png", ContentFile(data), save=False)
            if self.paper.is_student_private:
                self._private_files_written.append(target.name)
            record.save()
            order += 1

    @staticmethod
    def _read_figure(path):
        try:
            if path and os.path.isfile(path):
                with open(path, "rb") as handle:
                    return handle.read()
        except OSError:
            return None
        return None
