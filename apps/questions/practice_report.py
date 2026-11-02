"""Multipage Unicode practice report. Noto's OFL licence is in static/fonts/."""
import html
import io
from pathlib import Path

import pymupdf as fitz
from django.conf import settings


def _text(value):
    return html.escape(str(value if value is not None else ""), quote=True).replace("\n", "<br>")


def generate_practice_report(attempt, result):
    font_dir = Path(settings.BASE_DIR) / "static" / "fonts"
    archive = fitz.Archive(str(font_dir))
    images = {}

    def image_tag(field, alt):
        if not field or not field.name:
            return ""
        if field.name not in images:
            try:
                with field.open("rb") as source:
                    data = source.read()
                alias = f"practice-figure-{len(images)}.png"
                archive.add(data, alias)
                images[field.name] = alias
            except (OSError, ValueError):
                return '<p class="muted">[Image unavailable]</p>'
        return f'<p><img src="{images[field.name]}" alt="{_text(alt)}" width="200"></p>'

    counts = result["counts"]
    score = f'{attempt.score_obtained} / {result["graded_marks"]}' if result["keyed_count"] else "Not graded"
    percent = f'{result["percentage"]}%' if result["percentage"] is not None else "Not graded"
    accuracy = f'{result["accuracy"]}%' if result["accuracy"] is not None else "Not available"
    parts = [f'''<h1>Practice report / अभ्यास परिणाम</h1>
        <p class="muted">{_text(attempt.exam.title)} · {_text(attempt.started_at.strftime("%d %b %Y %H:%M"))}</p>
        <table><tr><th>Score on confirmed keys</th><th>Percentage</th><th>Accuracy</th><th>Outcome</th></tr>
        <tr><td>{_text(score)}</td><td>{_text(percent)}</td><td>{_text(accuracy)}</td><td>{_text(result["outcome"])}</td></tr></table>
        <p>Total possible marks: {_text(attempt.total_marks)} | Confirmed-key marks: {_text(result["graded_marks"])}<br>
        Correct: {counts["correct"]} | Incorrect: {counts["incorrect"]} | Unattempted: {counts["unattempted"]} | Answered but ungraded: {counts["ungraded"]}<br>
        Elapsed time: {attempt.time_taken_seconds} sec | Recorded question time: {result["time_logged_seconds"]} sec</p>
        <p class="muted">Only student-confirmed, unambiguous keys are scored. Unknown keys are not graded. Question time reflects saved browser activity and may be incomplete. Private practice is not an institutional qualification.</p>''']
    for kind, groups in result["breakdowns"].items():
        parts.append(f"<h2>{_text(kind.title())} performance</h2>")
        for group in groups:
            acc = f'{group["accuracy"]}%' if group["accuracy"] is not None else "No graded answers"
            parts.append(f'<p><b>{_text(group["name"])}</b> — {_text(acc)}; '
                         f'{group["correct"]} correct, {group["incorrect"]} incorrect, '
                         f'{group["unattempted"]} unattempted, {group["ungraded"]} ungraded</p>')
    parts.append("<h2>Question-wise analysis / प्रश्न अनुसार विवरण</h2>")
    for row in result["rows"]:
        q, response = row["question"], row["response"]
        parts.append(f'<div class="question"><h3>Question {response.order} — {_text(row["label"])}</h3>'
                     f'<p>{_text(q.question_text)}</p>')
        parts.append(image_tag(q.private_image, "Question image"))
        for fig in q.figures.all():
            if not fig.option_id:
                parts.append(image_tag(fig.private_image, "Question figure"))
        for option in row["options"]:
            flags = []
            if option.pk == response.selected_option_id:
                flags.append("your answer")
            if row["correct"] and option.pk == row["correct"].pk:
                flags.append("confirmed key")
            flag_text = f' ({", ".join(flags)})' if flags else ""
            parts.append(f'<p class="option"><b>{_text(option.option_key)}.</b> {_text(option.option_text)}{_text(flag_text)}</p>')
            for fig in option.figures.all():
                parts.append(image_tag(fig.private_image, "Option image"))
        parts.append(f'<p class="muted">Recorded time: {response.time_spent_seconds} sec'
                     + (f' | Marks: {_text(response.marks_awarded)}' if row["correct"] else "") + '</p>')
        if row["explanation"]:
            parts.append(f'<p><b>Explanation:</b> {_text(row["explanation"])}</p>')
        parts.append('</div>')
    css = '''
      @font-face { font-family: NotoDevanagari; src: url(NotoSansDevanagari.ttf); }
      body { font-family: NotoDevanagari; font-size: 10pt; color: #172444; line-height: 1.35; }
      h1 { font-size: 21pt; color: #1d3f99; margin-bottom: 8pt; }
      h2 { font-size: 14pt; color: #1d3f99; margin-top: 16pt; }
      h3 { font-size: 11pt; color: #172444; margin-bottom: 4pt; }
      p { margin-top: 4pt; margin-bottom: 5pt; }
      .muted { color: #546278; font-size: 9pt; }
      .option { margin-left: 12pt; }
      .question { border-top: 1px solid #dbe4f1; padding-top: 6pt; }
      table { width: 100%; border-collapse: collapse; margin-top: 10pt; }
      th, td { border: 1px solid #dbe4f1; padding: 6pt; text-align: left; }
      th { background: #eef3ff; }
      img { max-width: 200pt; max-height: 180pt; }
    '''
    buffer = io.BytesIO()
    writer = fitz.DocumentWriter(buffer)
    story = fitz.Story(html="".join(parts), user_css=css, archive=archive)
    page = fitz.paper_rect("a4")
    story.write(writer, lambda number, filled: (page, page + (38, 38, -38, -38), None))
    writer.close()
    return buffer.getvalue()
