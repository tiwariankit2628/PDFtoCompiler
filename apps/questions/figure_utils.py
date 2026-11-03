"""Safe rendering of page previews and crops from the original PDF."""
import math
import pymupdf as fitz


def validate_box(box):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        raise ValueError("Select a rectangular area on the page.")
    try:
        x0, y0, x1, y1 = (float(value) for value in box)
    except (TypeError, ValueError):
        raise ValueError("Invalid crop coordinates.") from None
    if (not all(math.isfinite(v) and 0 <= v <= 1 for v in (x0, y0, x1, y1))
            or x1 - x0 < .002 or y1 - y0 < .002):
        raise ValueError("Crop must be a non-empty rectangle within the page.")
    return [x0, y0, x1, y1]


def page_image(page, box=None, max_side=2200, scale=2):
    rect = page.rect
    if rect.width <= 0 or rect.height <= 0 or rect.width > 20000 or rect.height > 20000:
        raise ValueError("Unsupported PDF page dimensions.")
    if box is not None:
        x0, y0, x1, y1 = validate_box(box)
        clip = fitz.Rect(rect.x0 + x0 * rect.width, rect.y0 + y0 * rect.height,
                         rect.x0 + x1 * rect.width, rect.y0 + y1 * rect.height)
    else:
        clip = rect
    factor = min(scale, max_side / max(clip.width, clip.height))
    pix = page.get_pixmap(matrix=fitz.Matrix(factor, factor), clip=clip, alpha=False)
    return pix.tobytes("png")
