"""Conservative candidate regions in locally OCRed raster pages.

Only pixels from the original page are inspected. This is not an image
recognition model: imperfect candidates must be reviewed against the PDF.
"""
from collections import deque
import io

import pymupdf as fitz
from PIL import Image, ImageDraw, ImageFilter


def scanned_graphic_candidates(page, region, text_lines):
    """Find sizeable non-text ink clusters in a scanned question segment.

    Text boxes from Tesseract are masked only for detection; returned rectangles
    are cropped from the *unmodified* original PDF. Unrecognized text, tables,
    speckles and disconnected drawings can still confuse this heuristic.
    """
    region = fitz.Rect(region) & page.rect
    if region.is_empty or region.width < 30 or region.height < 30:
        return []
    # Bound per-region memory and work, including oversized PDF page boxes.
    scale = min(1.5, 1800 / max(region.width, region.height))
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=region, alpha=False)
        image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("L")
    except Exception:
        # A corrupt/unsupported image must not discard the question's text.
        return []
    mask = image.point(lambda value: 255 if value < 185 else 0)
    draw = ImageDraw.Draw(mask)
    for _, line, spans in text_lines:
        if not region.intersects(line):
            continue
        for span in spans:
            if not span.get("text", "").strip():
                continue
            box = fitz.Rect(span["bbox"]) & region
            if box.is_empty:
                continue
            coords = ((box.x0 - region.x0) * scale - 2, (box.y0 - region.y0) * scale - 2,
                      (box.x1 - region.x0) * scale + 2, (box.y1 - region.y0) * scale + 2)
            draw.rectangle(coords, fill=0)

    # About one pixel per PDF point; dilation connects neighbouring strokes,
    # but component boxes are still limited to the question segment.
    factor = min(1.0, 1000 / max(region.width, region.height))
    size = (max(1, round(region.width * factor)), max(1, round(region.height * factor)))
    mask = mask.resize(size, Image.Resampling.BOX).point(lambda value: 255 if value > 12 else 0)
    mask = mask.filter(ImageFilter.MaxFilter(7))
    width, height = mask.size
    pixels = bytearray(mask.tobytes())
    if sum(bool(value) for value in pixels) > len(pixels) * .45:
        return []  # dark/inverted/noisy page: do not create huge false crops
    found = []
    for start in range(len(pixels)):
        if not pixels[start]:
            continue
        pixels[start] = 0
        queue = deque([start])
        x0 = x1 = start % width
        y0 = y1 = start // width
        count = 0
        while queue:
            position = queue.popleft()
            x, y = position % width, position // width
            count += 1
            x0, x1, y0, y1 = min(x0, x), max(x1, x), min(y0, y), max(y1, y)
            for neighbor in (position - width if y else -1,
                             position + width if y + 1 < height else -1,
                             position - 1 if x else -1,
                             position + 1 if x + 1 < width else -1):
                if neighbor >= 0 and pixels[neighbor]:
                    pixels[neighbor] = 0
                    queue.append(neighbor)
        box_width, box_height = (x1 - x0 + 1) / factor, (y1 - y0 + 1) / factor
        if (box_width < 26 or box_height < 26 or count < 70
                or box_width * box_height > region.get_area() * .55
                or box_width * box_height > page.rect.get_area() * .25):
            continue
        found.append(fitz.Rect(region.x0 + x0 / factor, region.y0 + y0 / factor,
                               region.x0 + (x1 + 1) / factor, region.y0 + (y1 + 1) / factor) & region)
    # A noisy scan can yield thousands of fragments. Surface only a bounded set
    # of substantial candidates; unreturned material is still in the source PDF.
    found.sort(key=lambda box: box.get_area(), reverse=True)
    return sorted(found[:8], key=lambda box: (box.y0, box.x0))
