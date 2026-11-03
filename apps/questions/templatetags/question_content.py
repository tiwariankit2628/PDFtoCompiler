"""Safe shared question renderer: text, readable labels and structured tables."""
import re

from django import template
from django.core.exceptions import ValidationError
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

from ..structured_content import TABLE_MARKER, validate_tables

register = template.Library()
_LABEL = re.compile(r"\b(?:Assertion\s*(?:[A-Z]|\([A-Z]\))?|Reason\s*(?:[A-Z]|\([A-Z]\))?|Statement\s*(?:I{1,3}|[1-3]))\s*:", re.I)


def _text(value):
    parts, offset = [], 0
    for match in _LABEL.finditer(value):
        # Do not turn an introductory 'labelled as Reason R:' into a heading.
        if re.search(r"(?:labelled|labeled)\s+as\s+$", value[max(0, match.start()-25):match.start()], re.I):
            continue
        parts.append(str(escape(value[offset:match.start()])).replace("\n", "<br>"))
        parts.append(str(format_html('<strong class="statement-label">{}</strong>', match.group())))
        offset = match.end()
    parts.append(str(escape(value[offset:])).replace("\n", "<br>"))
    return "".join(parts)


def _table(table):
    rows = []
    for row in table["rows"]:
        cells = []
        for cell in row:
            tag = "th" if cell["header"] else "td"
            # tag, spans and structure were locally selected/validated, content escaped.
            cells.append(f'<{tag} colspan="{cell["colspan"]}" rowspan="{cell["rowspan"]}">{_text(cell["text"])}</{tag}>')
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return '<div class="question-table-wrap"><table class="question-table"><tbody>' + "".join(rows) + '</tbody></table></div>'


@register.filter
def render_question(question):
    text = getattr(question, "question_text", "") or ""
    try:
        tables = validate_tables(getattr(question, "tables", []) or [], text)
    except ValidationError:
        # Existing/corrupt rows must never turn into executable markup.
        return mark_safe(_text(text))
    parts, offset = [], 0
    for match in TABLE_MARKER.finditer(text):
        parts.append(_text(text[offset:match.start()]))
        parts.append(_table(tables[int(match.group(1)) - 1]))
        offset = match.end()
    parts.append(_text(text[offset:]))
    return mark_safe("".join(parts))
