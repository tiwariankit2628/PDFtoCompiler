"""Shared table editing and validation for staged and bank questions."""
import json

from django import forms
from django.core.exceptions import ValidationError

from .structured_content import validate_tables


def add_tables_editor(form, question):
    form._existing_tables = getattr(question, "tables", []) or []
    form.fields["tables_json"] = forms.CharField(
        required=False, label="Structured tables",
        widget=forms.Textarea(attrs={"rows": 5, "class": "table-json-editor", "data-table-editor": "true"}),
        help_text=("Edit the table cells below, or use the advanced JSON editor. "
                   "Keep each [[table:1]] marker in the question text where the table belongs. "
                   "Use $...$ or \\(...\\) for mathematical notation."),
        initial=json.dumps(form._existing_tables, ensure_ascii=False, indent=2),
    )
    if not form.is_bound:
        form.initial["tables_json"] = json.dumps(form._existing_tables, ensure_ascii=False, indent=2)


def clean_tables_editor(form, cleaned):
    # Older clients that do not send the new field retain existing tables.
    if "tables_json" not in form.data:
        tables = form._existing_tables
    else:
        try:
            tables = json.loads(cleaned.get("tables_json") or "[]")
        except (ValueError, TypeError):
            form.add_error("tables_json", "Enter valid table JSON or use the cell editor.")
            return cleaned
    try:
        cleaned["tables_data"] = validate_tables(tables, cleaned.get("question_text", ""))
    except ValidationError as exc:
        form.add_error("tables_json", exc)
    return cleaned
