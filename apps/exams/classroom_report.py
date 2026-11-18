"""Printable classroom summary that never invents a grade for missing keys."""
import html
import io
from pathlib import Path

import pymupdf as fitz
from django.conf import settings


def generate_classroom_report(attempt, summary, *, title="Classroom result summary"):
    def text(value):
        return html.escape(str(value if value is not None else ""), quote=True)

    counts = summary["counts"]
    score = str(attempt.score_obtained) if summary["score_available"] else "Not available"
    percentage = f'{summary["percentage"]}%' if summary["percentage"] is not None else "Not available"
    completeness = "Complete key recorded." if summary["fully_keyed"] else "This score or percentage may be partial, not an overall outcome."
    name = attempt.student.get_full_name() or attempt.student.username
    label = "confirmed keys" if title == "Classroom result summary" else "graded questions"
    content = f'''<h1>{text(title)}</h1>
    <p>{text(attempt.exam.title)}<br>Candidate: {text(name)}</p>
    <table><tr><th>Score on {label}</th><th>Percentage on {label}</th></tr>
      <tr><td>{text(score)}</td><td>{text(percentage)}</td></tr></table>
    <p>Correct: {counts["correct"]} | Incorrect: {counts["incorrect"]} | Answered, ungraded: {counts["ungraded"]} | Unattempted: {counts["unattempted"]}</p>
    <p>Without a confirmed key when graded: {summary["pending_keys"]}<br>Key status not recorded at grading: {summary["unknown_keys"]}</p>
    <p class="note">{text(completeness)} This report uses saved grading decisions. An ungraded answer is not marked wrong. Changing a key later does not automatically regrade an attempt.</p>'''
    css = '''@font-face { font-family: Noto; src: url(NotoSansDevanagari.ttf); }
      body { font-family:Noto; color:#202923; font-size:11pt; line-height:1.5; }
      h1 { font-size:22pt; color:#bd412a; } table { border-collapse:collapse; width:100%; }
      th,td { padding:8pt; border:1px solid #dce3d9; text-align:left; } th { background:#f2f5ee; }
      .note { color:#59675d; font-size:9pt; }'''
    archive = fitz.Archive(str(Path(settings.BASE_DIR) / "static" / "fonts"))
    story = fitz.Story(html=content, user_css=css, archive=archive)
    output = io.BytesIO()
    writer = fitz.DocumentWriter(output)
    page = fitz.paper_rect("a4")
    story.write(writer, lambda number, filled: (page, page + (42, 42, -42, -42), None))
    writer.close()
    return output.getvalue()
