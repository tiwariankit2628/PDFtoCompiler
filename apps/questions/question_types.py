"""Local, free heuristic detection of non-single-correct question types.

Many real question papers mix ordinary single-correct MCQs with question types
the MCQ bank cannot grade the same way: numerical / integer-value answers,
multiple-correct (one-or-more) choices, and subjective / essay questions. The
previous pipeline treated every item as a single-correct MCQ, so a numerical
question looked like a *broken* MCQ ("only 0 options detected") and a
multiple-correct question was silently accepted as single-correct.

This module inspects only the already-extracted statement and options. It never
invents answers, options or content and never changes grading. It returns a
best-effort type plus a human-readable reason so the reviewer is warned and can
confirm or correct the type before approval. Detection is deliberately
conservative: when the evidence is weak the item stays a single-correct MCQ and
the existing option checks apply.

No network, model weights, API key or paid service is used.
"""
import re

# These string values intentionally match ``ExtractedQuestion.DetectedType`` and
# ``Question.QuestionType`` so a detected type maps straight through on approval.
SINGLE = "mcq_single"
MULTIPLE = "mcq_multiple"
NUMERICAL = "numerical"
SUBJECTIVE = "subjective"


# Explicit, printed markers of a "one or more correct" question. We require an
# unambiguous phrase rather than guessing from plural wording such as "are
# correct", which appears in many ordinary single-answer questions.
_MULTIPLE_CORRECT = re.compile(
    r"\bone or more\b"
    r"|\bmore than one\b"
    r"|\b(?:select|choose|mark|tick)\s+all\s+that\s+apply\b"
    r"|\ball\s+that\s+are\s+correct\b"
    r"|\bmay\s+be\s+correct\b"
    r"|\bmultiple\s+(?:correct|answers?|options?)\b"
    r"|\b(?:one|more)\s+or\s+more\s+of\s+the\s+following\b"
    r"|एक\s*या\s*अधिक"
    r"|एक\s*से\s*अधिक",
    re.IGNORECASE,
)

# "Which statements is/are correct?" is ambiguous: it may describe genuine
# multi-select choices OR one choice containing a combination of statements.
_AMBIGUOUS_CORRECT = re.compile(r"\bis\s*/\s*are\s+correct\b", re.IGNORECASE)
_COMBINATION_REFERENCE = re.compile(r"(?<![A-Za-z0-9])(?:[A-E]|I{1,3}|IV|V|[1-5])(?![A-Za-z0-9])")
_COMBINATION_WORDS = re.compile(
    r"\b(?:and|or|only|both|either|neither|all|none|of|the|above|following|"
    r"statements?|options?|are|is|correct|true)\b", re.IGNORECASE,
)
_STATEMENT_CONTEXT = re.compile(r"\b(?:statements?|assertions?)\b", re.IGNORECASE)
_STATEMENT_LABEL = re.compile(r"(?<!\w)[A-E][.)](?=\s|$)")


def _combination_references(value):
    """Recognize a short choice made only of statement references/connectors.

    Do not use general prose or try to interpret the correctness of a
    statement. Printed letter references are case-sensitive to avoid matching
    ordinary articles; roman numerals and digits are supported as references.
    """
    if not isinstance(value, str) or len(value) > 100:
        return None
    references = _COMBINATION_REFERENCE.findall(value)
    rest = _COMBINATION_REFERENCE.sub(" ", value)
    rest = _COMBINATION_WORDS.sub(" ", rest)
    rest = re.sub(r"[\s,.;:()\[\]{}+&/\-–—]+", "", rest)
    if rest or not references:
        return None
    return frozenset(references)


def _combination_choice_set(text, options):
    """Strong evidence that the options are combinations, not statements.

    This is still a heuristic. Explicit one-or-more instructions take
    precedence, and extraction flags the conclusion for a person to verify.
    """
    if not (_STATEMENT_CONTEXT.search(text) or len(set(_STATEMENT_LABEL.findall(text))) >= 2):
        return False
    if not isinstance(options, list) or not 3 <= len(options) <= 5:
        return False
    references = [_combination_references(option.get("text", "")) if isinstance(option, dict) else None
                  for option in options]
    readable = [reference for reference in references if reference]
    return (len(readable) >= max(3, len(options) - 1)
            and sum(len(reference) >= 2 for reference in readable) >= 2
            and len(set(readable)) >= 2)


# Numerical / integer value answers. These are strong, answer-shaped cues that do
# not normally appear in prose MCQ stems.
_NUMERICAL = re.compile(
    r"\bnearest\s+integer\b"
    r"|\b(?:non[- ]?negative\s+)?integer\b"
    r"|\bnumeric(?:al)?\s+value\b"
    r"|\bvalue\s+type\b"
    r"|\brounded?\s+off\b"
    r"|\bround\s+your\s+answer\b"
    r"|\b(?:up\s*to|upto|correct\s+to|to)\s+\w+\s+decimal\s+place"
    r"|\banswer\s+is\s+an?\s+integer\b"
    r"|\bfill\s+in\s+the\s+(?:numerical\s+)?value\b"
    r"|निकटतम\s*पूर्णांक"
    r"|पूर्णांक\s*मान"
    r"|संख्यात्मक\s*मान",
    re.IGNORECASE,
)

# A fill-in blank (a run of underscores) anywhere in the statement, or a trailing
# dotted/ellipsis answer slot, indicates a fill-in answer rather than an MCQ.
# Underscores are a safe signal anywhere; dot leaders are only trusted at the end
# to avoid matching table-of-contents style leaders mid-text.
_FILL_BLANK = re.compile(r"_{3,}")
_TRAILING_BLANK = re.compile(r"(?:\.{5,}|…{2,}|…\s*…)\s*[.?]?\s*$")

# Subjective / essay instruction verbs, anchored at a clause boundary so an
# "...which describes..." style MCQ stem is not misread. Only consulted when the
# item has no usable options and no numerical cue.
_SUBJECTIVE = re.compile(
    r"(?:^|[\s.;:\-–—)])\s*"
    r"(?:explain|describe|discuss|derive|prove|justify|elaborate|illustrate|"
    r"comment\s+on|distinguish\s+between|differentiate\s+between|"
    r"write\s+(?:a\s+|short\s+|brief\s+)*(?:note|notes|essay|paragraph)|"
    r"state\s+and\s+explain|give\s+reasons?\b|in\s+your\s+own\s+words)"
    r"\b",
    re.IGNORECASE,
)
_SUBJECTIVE_HINDI = re.compile(
    r"समझाइए|समझाइये|व्याख्या\s*कीजिए|वर्णन\s*कीजिए|सिद्ध\s*कीजिए|"
    r"परिभाषित\s*कीजिए|कारण\s*दीजिए|टिप्पणी\s*लिखिए"
)


def _usable_option_count(options):
    """Count options that carry text (an image-only option has empty text)."""
    if not isinstance(options, list):
        return 0
    return sum(1 for option in options if isinstance(option, dict))


def classify_question(text, options):
    """Return ``(detected_type, reason)`` for a staged question.

    ``reason`` is a review note for non-single types and for a combination-style
    single-choice inference; otherwise it is empty. Content is never modified.
    """
    text = text or ""
    option_count = _usable_option_count(options)

    # With options present, explicit multi-select instructions take priority
    # over any apparent combination of statements. The ambiguous "is/are"
    # phrase alone remains MULTIPLE unless the *choices* strongly look like
    # combinations, in which case flag the single-choice inference for review.
    if option_count >= 2:
        if _MULTIPLE_CORRECT.search(text):
            return MULTIPLE, (
                "Multiple-correct question detected: the bank grades one correct "
                "option, so confirm the question type and intended answers before "
                "approval."
            )
        if _AMBIGUOUS_CORRECT.search(text):
            if _combination_choice_set(text, options):
                return SINGLE, (
                    "Choices appear to be combinations of statements. Check the "
                    "original and confirm that exactly one printed choice is selected."
                )
            return MULTIPLE, (
                "Multiple-correct question detected: the bank grades one correct "
                "option, so confirm the question type and intended answers before "
                "approval."
            )
        return SINGLE, ""

    # No usable options: this is either a broken MCQ (left to the existing option
    # checks) or a genuinely non-MCQ type. Prefer the most answer-shaped cue.
    if _NUMERICAL.search(text) or _FILL_BLANK.search(text) or _TRAILING_BLANK.search(text.strip()):
        return NUMERICAL, (
            "Numerical / integer-value question detected: no multiple-choice "
            "options are expected. Enter the accepted answer manually — this is "
            "not a broken MCQ."
        )
    if _SUBJECTIVE.search(text) or _SUBJECTIVE_HINDI.search(text):
        return SUBJECTIVE, (
            "Subjective / essay-type question detected: the MCQ bank cannot grade "
            "it automatically. Preserve its content and exclude it, or convert it "
            "to a supported type manually."
        )
    return SINGLE, ""
