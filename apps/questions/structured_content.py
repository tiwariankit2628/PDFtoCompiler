"""Bounded, non-HTML representation of tables embedded in question stems.

The text contains numbered [[table:1]] markers.  Cell contents are ordinary
text/LaTeX, never trusted HTML. Both the server and CBT renderers escape it.
"""
import re

from django.core.exceptions import ValidationError

TABLE_MARKER = re.compile(r"\[\[table:(\d+)\]\]", re.IGNORECASE)
MAX_TABLES, MAX_ROWS, MAX_COLUMNS, MAX_CELL_CHARS = 20, 100, 20, 4000


def validate_tables(tables, text=None):
    """Validate a rectangular grid including merged cells; return normalized data."""
    if not isinstance(tables, list) or len(tables) > MAX_TABLES:
        raise ValidationError("Tables must be a list of at most 20 tables.")
    normalized = []
    for table in tables:
        if not isinstance(table, dict) or set(table) - {"columns", "rows"}:
            raise ValidationError("A table must contain only columns and rows.")
        columns, rows = table.get("columns"), table.get("rows")
        if type(columns) is not int or not 1 <= columns <= MAX_COLUMNS:
            raise ValidationError("A table needs 1–20 columns.")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
            raise ValidationError("A table needs 1–100 rows.")
        occupied = set()
        clean_rows = []
        for ri, row in enumerate(rows):
            if not isinstance(row, list) or len(row) > columns:
                raise ValidationError("Invalid table row.")
            clean_row = []
            ci = 0
            for cell in row:
                while (ri, ci) in occupied:
                    ci += 1
                if not isinstance(cell, dict) or set(cell) - {"text", "colspan", "rowspan", "header"}:
                    raise ValidationError("Invalid table cell.")
                value = cell.get("text", "")
                colspan, rowspan = cell.get("colspan", 1), cell.get("rowspan", 1)
                header = cell.get("header", False)
                if not isinstance(value, str) or len(value) > MAX_CELL_CHARS:
                    raise ValidationError("Table cell text is invalid or too long.")
                if (type(colspan) is not int or type(rowspan) is not int
                        or not 1 <= colspan <= columns or not 1 <= rowspan <= len(rows)
                        or ci + colspan > columns or ri + rowspan > len(rows)
                        or type(header) is not bool):
                    raise ValidationError("Invalid table cell span.")
                for y in range(ri, ri + rowspan):
                    for x in range(ci, ci + colspan):
                        if (y, x) in occupied:
                            raise ValidationError("Table cells overlap.")
                        occupied.add((y, x))
                clean_row.append({"text": value, "colspan": colspan, "rowspan": rowspan, "header": header})
                ci += colspan
            # No holes: a missing cell would change the source layout silently.
            if any((ri, x) not in occupied for x in range(columns)):
                raise ValidationError("Every table row must fill its columns.")
            clean_rows.append(clean_row)
        normalized.append({"columns": columns, "rows": clean_rows})
    if text is not None:
        raw_markers = [m.group(1) for m in TABLE_MARKER.finditer(text or "")]
        # Avoid integer-conversion errors on maliciously large digit strings.
        if any(len(marker) > 3 for marker in raw_markers):
            raise ValidationError("A table marker number is invalid.")
        markers = [int(marker) for marker in raw_markers]
        if sorted(markers) != list(range(1, len(normalized) + 1)):
            raise ValidationError("Include each table marker exactly once, for example [[table:1]]. Remove a table and its marker together.")
    return normalized
