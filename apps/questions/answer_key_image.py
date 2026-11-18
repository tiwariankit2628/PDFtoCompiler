"""Read answer-key photos locally as *unconfirmed* suggestions.

No PDF, photo or OCR text is sent to a provider or persisted by this module.
A human must compare every suggestion with the image before using it. Parsing is
intentionally conservative: unsupported, duplicate and ambiguous entries are
left blank rather than guessed. The calling view enforces ownership and CSRF.
"""
import io
import os
import re
import statistics
from bisect import bisect_left
from collections import Counter, defaultdict

import pymupdf as fitz
from PIL import Image, ImageOps, UnidentifiedImageError


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
MAX_IMAGES = 3
# Retry only a few well-aligned columns, with a bounded total raster area.
MAX_COLUMN_RETRIES = 4
MAX_RETRY_PIXELS = 12_000_000
NUMERIC_KEYS = {"1": "A", "2": "B", "3": "C", "4": "D", "5": "E"}


class AnswerImageError(ValueError):
    """A safe, user-facing image validation or recognition error."""


def _english_data_directory():
    """Use only an existing local language installation; never download one."""
    candidates = [os.environ.get("TESSDATA_PREFIX", "")]
    # Windows installations often keep data beside their configured binary.
    try:
        from django.conf import settings
        command = getattr(settings, "TESSERACT_CMD", "")
        if command:
            candidates.append(os.path.join(os.path.dirname(command), "tessdata"))
    except (ImportError, RuntimeError):
        pass
    # The pip-installed offline language data lives in the active Python
    # environment (including Windows venvs); no executable or download is
    # needed at recognition time. Environment/operator overrides come first.
    try:
        import tessdata
        candidates.append(tessdata.data_path())
    except (ImportError, OSError, RuntimeError):
        pass
    for directory in candidates:
        if directory and os.path.isfile(os.path.join(directory, "eng.traineddata")):
            return directory
    # Consult the executable only if no configured or pip data exists. This
    # avoids requiring a system command (or emitting warnings about an empty
    # PATH) for normal answer-key image recognition.
    try:
        directory = fitz.get_tessdata()
        if directory and os.path.isfile(os.path.join(directory, "eng.traineddata")):
            return directory
    except (RuntimeError, OSError, AttributeError):
        pass
    raise AnswerImageError(
        "Local English OCR data is missing on this server. Install the project requirements "
        "and restart the app, or set TESSDATA_PREFIX to a folder containing eng.traineddata. "
        "You can still type or paste the key."
    )


def _image_bytes(upload):
    if getattr(upload, "size", 0) > MAX_IMAGE_BYTES:
        raise AnswerImageError("Each answer-key image must be 8 MB or smaller.")
    data = upload.read(MAX_IMAGE_BYTES + 1)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise AnswerImageError("Each answer-key image must be a non-empty file of 8 MB or less.")
    return data


def _prepare_image(data):
    """Decode a bounded, single-frame JPEG/PNG and return a temporary RGB PNG."""
    try:
        with Image.open(io.BytesIO(data)) as source:
            if source.format not in ("JPEG", "PNG"):
                raise AnswerImageError("Choose a JPEG or PNG answer-key image.")
            width, height = source.size
            if width < 30 or height < 30 or width * height > MAX_IMAGE_PIXELS:
                raise AnswerImageError("The image is too small or too large. Use a clear photo up to 12 megapixels.")
            if getattr(source, "n_frames", 1) != 1:
                raise AnswerImageError("Animated images are not supported. Choose a single photo or screenshot.")
            source.verify()
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            # Composite transparency against white, not black (common for PNGs).
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                image = Image.alpha_composite(background, rgba).convert("RGB")
            else:
                image = image.convert("RGB")
            image = ImageOps.autocontrast(ImageOps.grayscale(image))
            # Small screenshots benefit from higher-resolution OCR. Bound the
            # scaled area as well as the original to avoid excessive memory use.
            if image.width < 2200 and image.width * image.height * 4 <= MAX_IMAGE_PIXELS:
                image = image.resize((image.width * 2, image.height * 2), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.convert("RGB").save(output, format="PNG")
            return output.getvalue()
    except AnswerImageError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise AnswerImageError("That file is not a readable JPEG or PNG image.") from None


def _ocr_words(image_data, tessdata):
    """Return local OCR words in source-image pixel coordinates, in memory."""
    try:
        pixmap = fitz.Pixmap(image_data)
        pdf = pixmap.pdfocr_tobytes(language="eng", tessdata=tessdata)
        with fitz.open(stream=pdf, filetype="pdf") as document:
            # The OCR PDF uses points, not pixels. Preserve the transform so
            # column retries can be cropped from the original prepared raster.
            xscale, yscale = pixmap.xres / 72.0, pixmap.yres / 72.0
            return [(word[0] * xscale, word[1] * yscale, word[2] * xscale, word[3] * yscale, *word[4:])
                    for word in document[0].get_text("words", sort=True)]
    except AnswerImageError:
        raise
    except Exception:
        # Do not expose filesystem paths or photo contents to website users.
        raise AnswerImageError(
            "Local image reading could not run. Check the PyMuPDF installation and English OCR data, "
            "restart the server, or enter the key manually. No image was sent to a provider."
        ) from None


def _rows(words):
    """Group OCR words by their vertical position, retaining their x positions."""
    positioned = []
    for word in words:
        if len(word) < 5 or not str(word[4]).strip():
            continue
        try:
            x0, y0, x1, y1 = (float(value) for value in word[:4])
        except (TypeError, ValueError):
            continue
        if y1 <= y0:
            continue
        positioned.append((x0, y0, x1, y1, str(word[4]).strip()))
    positioned.sort(key=lambda item: ((item[1] + item[3]) / 2, item[0]))
    result = []
    for word in positioned:
        center = (word[1] + word[3]) / 2
        height = word[3] - word[1]
        if result and abs(center - result[-1]["center"]) <= max(2, min(height, result[-1]["height"]) * 0.55):
            row = result[-1]
            row["words"].append(word)
            row["center"] = (row["center"] * (len(row["words"]) - 1) + center) / len(row["words"])
        else:
            result.append({"center": center, "height": height, "words": [word]})
    return [sorted(row["words"], key=lambda item: item[0]) for row in result]


def _label(token):
    return re.sub(r"[^A-Z]", "", token.upper())


QUESTION_LABELS = {"Q", "QNO", "QNUM", "QUESTION", "QUESTIONS", "QUESTIONNO", "QUESTIONNUMBER", "QUES", "NO", "NUMBER", "SNO"}
ANSWER_LABELS = {"ANS", "ANSWER", "ANSWERS", "ANSKEY", "KEY", "OPTION", "OPTIONS"}
PAIR = re.compile(r"^(?:Q(?:UESTION)?)?\s*(\d{1,5})\s*[:.\-=]\s*\(?([A-E1-5])\)?[,.]?$", re.I)
QUESTION = re.compile(r"^(?:Q(?:UESTION)?)?\s*(\d{1,5})[.),:\-]?$", re.I)
ANSWER = re.compile(r"^\(?([A-E1-5])\)?[.,]?$", re.I)


def _number(token):
    match = QUESTION.fullmatch(token)
    return int(match.group(1)) if match and int(match.group(1)) > 0 else None


def _answer(token):
    match = ANSWER.fullmatch(token)
    return match.group(1).upper() if match else None


def _is_pair_layout(words):
    """Require visual separation of multiple unlabelled Q/answer pairs."""
    if len(words) <= 2:
        return True
    starts = [word[0] for word in words]
    within = [starts[i + 1] - starts[i] for i in range(0, len(starts), 2)]
    between = [starts[i + 2] - starts[i + 1] for i in range(0, len(starts) - 2, 2)]
    return bool(between and min(within) > 0 and min(between) > statistics.median(within) * 1.25)


def parse_positioned_words(words, *, include_issues=False, question_x_max=None):
    """Return [(question number, printed answer)] without validating the paper.

    Supported layouts: explicit pairs (``1:B`` or ``1. (3)``), two-column
    tables, repeated Q/answer columns, and labelled horizontal Q/answer rows.
    An unlabelled run of numbers without pair spacing is intentionally ignored.
    OCR can still confidently misread a character, so every result is untrusted.
    """
    rows = _rows(words)
    pairs = []
    unclear_numbers = set()
    unreadable_rows = 0

    def note_unreadable(tokens, row=None):
        nonlocal unreadable_rows
        if not any(re.search(r"\d", token) for token in tokens):
            return
        unreadable_rows += 1
        # If a stable table identifies the question-number side, an unreadable
        # answer such as '3)' must not incorrectly flag question 3 elsewhere.
        if row is not None and question_x_max is not None:
            tokens = [word[4] for word in row if word[0] < question_x_max]
        # Only flag plausible printed question numbers; never repair a token
        # such as '#4' into '4' and never turn it into an answer suggestion.
        for token in tokens:
            match = re.match(r"^(?:Q(?:UESTION)?)?(\d{1,5})(?=$|[.),:=\-])", token, re.I)
            if match and int(match.group(1)) > 0:
                unclear_numbers.add(int(match.group(1)))

    header_seen = False
    index = 0
    while index < len(rows):
        row = [word for word in rows[index] if word[4] not in {"|", "—", "–"}]
        tokens = [word[4] for word in row]
        labels = [_label(token) for token in tokens]
        has_q = any(label in QUESTION_LABELS for label in labels)
        has_answer = any(label in ANSWER_LABELS for label in labels)

        # A horizontal grid needs explicit row labels and equal lengths; do not
        # guess which numeric row is the question row.
        if has_q and not has_answer and index + 1 < len(rows):
            following = rows[index + 1]
            next_labels = [_label(word[4]) for word in following]
            if any(label in ANSWER_LABELS for label in next_labels):
                q_tokens = [word[4] for word in row if _label(word[4]) not in QUESTION_LABELS]
                a_tokens = [word[4] for word in following if _label(word[4]) not in ANSWER_LABELS]
                numbers = [_number(token) for token in q_tokens]
                answers = [_answer(token) for token in a_tokens]
                if (numbers and len(numbers) == len(answers)
                        and all(number is not None for number in numbers)
                        and all(answer is not None for answer in answers)):
                    pairs.extend(zip(numbers, answers))
                else:
                    note_unreadable(q_tokens)
                index += 2
                continue
        if has_q and has_answer:
            header_seen = True
            index += 1
            continue

        # A whole row must be understandable; this avoids partially reading a
        # multi-answer entry such as "1 A C" as a single-correct answer.
        items = []
        position = 0
        while position < len(row):
            token = row[position][4]
            combined = PAIR.fullmatch(token)
            if combined:
                items.append((int(combined.group(1)), combined.group(2).upper(), True))
                position += 1
                continue
            number = _number(token)
            if position + 1 >= len(row):
                items = []
                break
            answer = _answer(row[position + 1][4])
            if number is None or answer is None:
                items = []
                break
            items.append((number, answer, False))
            position += 2
        all_combined = bool(items) and all(item[2] for item in items)
        spaced = bool(items) and len(row) % 2 == 0 and _is_pair_layout(row)
        separated_single = bool(items) and len(items) == 1 and (
            not items[0][1].isdigit() or header_seen or all_combined
            or bool(re.search(r"[.),:=\-]$", row[0][4]))
            # "9 (1)" is a pair even when OCR omits the number's period;
            # plain "9 1" remains ambiguous without a question/answer header.
            or len(row) == 2 and bool(re.fullmatch(r"\([A-E1-5]\)[.,]?", row[1][4], re.I))
        )
        # An unlabelled "1 2" or evenly spaced "1 2 3 4" may be a
        # question-number row, not Q/answer pairs. Do not infer its direction.
        accepted = bool(items) and (
            all_combined or separated_single
            or (len(items) > 1 and (spaced or any(item[2] for item in items)))
        )
        if accepted:
            pairs.extend((number, answer) for number, answer, _ in items)
        else:
            note_unreadable(tokens, row)
        index += 1
    if include_issues:
        return pairs, sorted(unclear_numbers), unreadable_rows
    return pairs


def _repeated_pair_columns(words):
    """Find stable repeated Q/answer columns; return None for uncertain layouts.

    A single bad cell must not discard the other cells on that row. We only
    split when at least three complete, well-spaced rows establish the same
    columns. We do not infer a missing number or answer from their sequence.
    """
    candidates = []
    for row in _rows(words):
        row = [word for word in row if word[4] not in {"|", "—", "–"}]
        if not (4 <= len(row) <= 12 and len(row) % 2 == 0 and _is_pair_layout(row)):
            continue
        if not all(_number(row[i][4]) and _answer(row[i + 1][4]) for i in range(0, len(row), 2)):
            continue
        # Distinguish a question/answer table from an unlabelled numeric grid.
        if not all(re.search(r"[.),:\-]$", row[i][4]) or row[i][4].upper().startswith("Q")
                   or not _answer(row[i + 1][4]).isdigit()
                   or re.fullmatch(r"\([1-5]\)[.,]?", row[i + 1][4])
                   for i in range(0, len(row), 2)):
            continue
        candidates.append(row)
    if not candidates:
        return None
    size, count = Counter(len(row) for row in candidates).most_common(1)[0]
    if count < 3:
        return None
    candidates = [row for row in candidates if len(row) == size]
    count_columns = size // 2
    height = statistics.median(word[3] - word[1] for row in candidates for word in row)
    centers = [statistics.median(row[2 * i][0] for row in candidates) for i in range(count_columns)]
    gaps = [right - left for left, right in zip(centers, centers[1:])]
    if not gaps or min(gaps) <= 0:
        return None
    tolerance = max(height * 3, min(gaps) * 0.15)
    aligned = [row for row in candidates
               if all(abs(row[2 * i][0] - centers[i]) <= tolerance for i in range(count_columns))]
    if len(aligned) < 3:
        return None
    # Cuts lie in the observed gaps between an answer and the next number.
    cuts = [statistics.median((row[2 * i + 1][2] + row[2 * i + 2][0]) / 2 for row in aligned)
            for i in range(count_columns - 1)]
    if any(cuts[i] >= cuts[i + 1] for i in range(len(cuts) - 1)):
        return None
    ys = sorted(statistics.median((word[1] + word[3]) / 2 for word in row) for row in aligned)
    spacings = [b - a for a, b in zip(ys, ys[1:]) if b > a]
    if not spacings:
        return None
    step = min(statistics.median(spacings), height * 3.5)
    columns = []
    for i in range(count_columns):
        q_right = statistics.median(row[2 * i][2] for row in aligned)
        a_left = statistics.median(row[2 * i + 1][0] for row in aligned)
        if q_right >= a_left:
            return None
        columns.append({
            "left": max(cuts[i - 1] if i else 0, min(row[2 * i][0] for row in aligned) - 2 * height),
            "right": min(cuts[i] if i < len(cuts) else float("inf"),
                         max(row[2 * i + 1][2] for row in aligned) + 2 * height),
            "top": ys[0] - 2.5 * step,
            "bottom": ys[-1] + 2.5 * step,
            "question_x_max": (q_right + a_left) / 2,
            "height": height,
            "step": step,
        })
    return cuts, columns


def _numbered_row_index(words, column):
    """Index unambiguous source labels by y for efficient retry comparison."""
    observed = []
    for row in _rows(words):
        numbers = set()
        for word in row:
            if word[0] >= column["question_x_max"]:
                continue
            combined = PAIR.fullmatch(word[4])
            number = int(combined.group(1)) if combined else _number(word[4])
            if number:
                numbers.add(number)
        if len(numbers) == 1:
            center = statistics.median((word[1] + word[3]) / 2 for word in row)
            observed.append((center, next(iter(numbers))))
    observed.sort()
    return [item[0] for item in observed], [item[1] for item in observed]


def _number_near_row(index, y, column):
    """Read, but never repair, a primary question label at a retry row's y."""
    centers, numbers = index
    position = bisect_left(centers, y)
    candidates = [(abs(centers[i] - y), numbers[i]) for i in (position - 1, position)
                  if 0 <= i < len(centers)]
    tolerance = min(column["step"] * 0.45, column["height"] * 1.2)
    candidates = [candidate for candidate in candidates if candidate[0] <= tolerance]
    return min(candidates)[1] if candidates else None


def _read_repeated_columns(image, words, tessdata, question_numbers):
    """Parse independent columns and cautiously retry only problematic crops.

    Retried readings are suggestions, not a second copy of a key. Conflicting
    readings or different question labels in the same position stay unclear;
    duplicate numbers printed within or across columns stay duplicates.
    """
    detected = _repeated_pair_columns(words)
    if not detected:
        return parse_positioned_words(words, include_issues=True)
    cuts, columns = detected
    per_column = [[] for _ in columns]
    for word in words:
        if len(word) < 5:
            continue
        try:
            center = (float(word[0]) + float(word[2])) / 2
        except (TypeError, ValueError):
            continue
        per_column[sum(center >= cut for cut in cuts)].append(word)

    primary = []
    retries = []
    for index, (column, column_words) in enumerate(zip(columns, per_column)):
        found, unclear, unreadable = parse_positioned_words(
            column_words, include_issues=True, question_x_max=column["question_x_max"])
        primary.append([found, set(unclear), unreadable])
        numbers = [number for number, _ in found]
        unique = set(numbers)
        lower, upper = (min(numbers), max(numbers)) if numbers else (0, -1)
        missing_between = any(lower <= n <= upper and n not in unique for n in question_numbers)
        if unreadable or set(numbers) - question_numbers or len(unique) < len(numbers) or missing_between:
            retries.append(index)
    retries = retries[:MAX_COLUMN_RETRIES]

    try:
        with Image.open(io.BytesIO(image)) as source:
            source = source.convert("RGB")
            boxes = {}
            for i in retries:
                column = columns[i]
                box = (max(0, int(column["left"])), max(0, int(column["top"])),
                       min(source.width, int(column["right"] + 1)),
                       min(source.height, int(column["bottom"] + 1)))
                if box[2] - box[0] >= 30 and box[3] - box[1] >= 30:
                    boxes[i] = box
            area = sum((box[2] - box[0]) * (box[3] - box[1]) for box in boxes.values())
            scale = 2 if area * 4 <= MAX_RETRY_PIXELS else 1
            remaining_pixels = MAX_RETRY_PIXELS
            for i, box in boxes.items():
                pixels = (box[2] - box[0]) * (box[3] - box[1]) * scale ** 2
                if pixels > remaining_pixels:
                    continue
                remaining_pixels -= pixels
                crop = source.crop(box)
                if scale == 2:
                    crop = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
                output = io.BytesIO()
                crop.save(output, format="PNG")
                try:
                    retry_words = _ocr_words(output.getvalue(), tessdata)
                except AnswerImageError:
                    # A failed optional retry must not discard the first pass.
                    continue
                column = columns[i]
                cutoff = (column["question_x_max"] - box[0]) * scale
                alternates, alternate_unclear, conflicts = [], set(), set()
                original_rows = _numbered_row_index(per_column[i], column)
                for row in _rows(retry_words):
                    found, unclear, _ = parse_positioned_words(row, include_issues=True, question_x_max=cutoff)
                    center = statistics.median((word[1] + word[3]) / 2 for word in row) / scale + box[1]
                    original_number = _number_near_row(original_rows, center, column)
                    if unclear and original_number is not None:
                        # A stray retry token (e.g. a page number) must not
                        # flag a different question in another column. If it
                        # overlaps a real source row, flag that source instead.
                        alternate_unclear.add(original_number)
                    for number, answer in found:
                        if original_number is not None and original_number != number:
                            conflicts.update((original_number, number))
                        else:
                            alternates.append((number, answer))
                primary_pairs, primary_unclear, unreadable = primary[i]
                primary_unclear.update(alternate_unclear | conflicts)
                by_number = defaultdict(list)
                for number, answer in primary_pairs:
                    by_number[number].append(answer)
                alternative_by_number = defaultdict(list)
                for number, answer in alternates:
                    alternative_by_number[number].append(answer)
                for number, answers in alternative_by_number.items():
                    existing = by_number.get(number, [])
                    if len(answers) > 1:
                        # Never collapse duplicates from either reading.
                        primary_pairs.extend([(number, answer) for answer in answers] if not existing
                                             else [(number, answers[0])])
                    elif not existing:
                        primary_pairs.append((number, answers[0]))
                    elif len(existing) == 1 and existing[0] != answers[0]:
                        primary_unclear.add(number)
                primary[i][2] = unreadable + bool(conflicts)
    except (OSError, ValueError, Image.DecompressionBombError):
        # The first pass is still usable if a bounded retry cannot be prepared.
        pass

    all_pairs, all_unclear, unreadable_rows = [], set(), 0
    for found, unclear, unreadable in primary:
        all_pairs.extend(found)
        all_unclear.update(unclear)
        unreadable_rows += unreadable
    return all_pairs, sorted(all_unclear), unreadable_rows


def suggest_for_questions(pairs, questions, *, exclude_approved=False, unclear_numbers=()):
    """Map readable entries to eligible questions, never accepting duplicates."""
    questions = list(questions)
    counts = Counter(question.question_number for question in questions)
    occurrences = defaultdict(list)
    for number, answer in pairs:
        occurrences[number].append(answer)
    available_numbers = set(counts)
    unclear_numbers = set(unclear_numbers)
    unknown = sorted((set(occurrences) | unclear_numbers) - available_numbers)
    rows = []
    for question in questions:
        number = question.question_number
        option_keys = []
        invalid_choices = not isinstance(question.options, list)
        for option in question.options if isinstance(question.options, list) else []:
            key = str(option.get("key", "")).upper().strip() if isinstance(option, dict) else ""
            if len(key) != 1 or key not in "ABCDE" or key in option_keys:
                invalid_choices = True
            else:
                option_keys.append(key)
        option_keys.sort()
        invalid_choices = invalid_choices or len(option_keys) < 2
        suggestions = occurrences.get(number, [])
        status, answer = "missing", ""
        if counts[number] != 1:
            status = "ambiguous_question"
        elif question.detected_type != "mcq_single":
            status = "unsupported_type"
        elif exclude_approved and (question.status == "approved" or question.approved_question_id):
            status = "already_approved"
        elif invalid_choices:
            status = "invalid_choices"
        elif len(suggestions) > 1:
            status = "duplicate"
        elif number in unclear_numbers:
            status = "unclear"
        elif suggestions:
            candidate = NUMERIC_KEYS.get(suggestions[0], suggestions[0])
            if candidate in option_keys:
                status, answer = "detected", candidate
            else:
                status = "invalid_option"
        rows.append({"number": number, "options": option_keys, "answer": answer, "status": status,
                     "existing_key": (question.correct_option or "").upper(),
                     "existing_confirmed": (question.answer_status in ("student_confirmed", "manually_verified")
                         and (question.detected_type == "mcq_single" or getattr(question, "typed_key_verified", False)))})
    return {"rows": rows, "unmatched_numbers": unknown[:50], "unmatched_count": len(unknown)}


def preview_answer_images(uploads, questions, *, exclude_approved=False):
    """Decode up to three photos in memory; return only unconfirmed suggestions."""
    uploads = list(uploads)
    if not uploads or len(uploads) > MAX_IMAGES:
        raise AnswerImageError("Choose between one and three JPEG or PNG images.")
    if sum(getattr(upload, "size", 0) for upload in uploads) > MAX_TOTAL_BYTES:
        raise AnswerImageError("The combined answer-key images must be 20 MB or smaller.")
    # Validate actual bytes (not just upload metadata) before doing expensive
    # decoding or OCR. No partial result is saved.
    raw_images = [_image_bytes(upload) for upload in uploads]
    if sum(len(data) for data in raw_images) > MAX_TOTAL_BYTES:
        raise AnswerImageError("The combined answer-key images must be 20 MB or smaller.")
    images = [_prepare_image(data) for data in raw_images]
    tessdata = _english_data_directory()
    questions = list(questions)
    question_numbers = {question.question_number for question in questions}
    pairs, unclear_numbers, unreadable_rows = [], set(), 0
    for image in images:
        words = _ocr_words(image, tessdata)
        found, unclear, unreadable = _read_repeated_columns(image, words, tessdata, question_numbers)
        pairs.extend(found)
        unclear_numbers.update(unclear)
        unreadable_rows += unreadable
    result = suggest_for_questions(pairs, questions, exclude_approved=exclude_approved,
                                   unclear_numbers=unclear_numbers)
    result["detected_count"] = sum(row["status"] == "detected" for row in result["rows"])
    result["unreadable_rows"] = unreadable_rows
    return result
