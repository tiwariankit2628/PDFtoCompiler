# PDF-to-CBT upgrade: validation and limitations

This report records the work and tests performed in the current development session. It is not a claim that every question in any PDF is correct or that the application is production-ready.

## What changed

- Local digital-PDF extraction now retains question page/normalized regions, detects some ruled tables and merged cells, conservatively reconstructs certain superscripts, fractions and simple radicals, and flags uncertain mathematical/table content for review. Automatic figures are original-PDF crops; reviewers can attach, replace, retarget or crop multiple question/option figures. Cloud extraction validates optional structured tables and source-region suggestions, with human review required.
- Structured tables are escaped and rendered as real responsive HTML, with math rendering in cells and options, in staging, bank, CBT and results. The cell editor supports adding/removing rows and an advanced representation for spans. Assertion–Reason and statement labels are visually distinguished when recognized.
- Optional Gemini answer suggestions are consent-gated, model-configurable, validated and kept separate from grading keys. Reviewers can accept, edit or reject them. Missing/low-confidence/vision-dependent answers remain unconfirmed. The staging page offers a bounded queue of up to five **separate** requests; unchanged pending/suggested requests are deduplicated. Student plans provide configurable quotas; provider calls, reported tokens and optional cost estimates are recorded.
- Private practice remains owner-scoped with private files, server-side fixed expiry, reattempts and confirmed-key-only scoring. Results distinguish correct, incorrect, unattempted and answered-but-ungraded, show attempted totals and excluded keys, and include subject/topic, time and report views. Student bank edits use copy-on-write to protect existing private-test history. Institutional exam settings are not changed by a private test's duration. Razorpay remains test-mode-only with server verification.

## The supplied original paper — local only

The 26-page sample was processed locally; it was not sent to Mathpix, Gemini or any other provider. The independent check compared explicit `Q<number>` labels from the PDF text layer against the questions assigned to each page, rather than simply counting extractor output:

| PDF pages | Labels and detected questions by page |
| --- | --- |
| 1–6 | 0, 9, 7, 8, 5, 8 |
| 7–12 | 7, 7, 8, 10, 10, 9 |
| 13–18 | 6, 6, 8, 9, 8, 7 |
| 19–24 | 10, 8, 8, 7, 8, 6 |
| 25–26 | 1, 0 |

Across those pages, all **180 explicit labels** matched an extracted question on the same start page; each detected question had at least two uniquely labelled options. This checks numbering and gross option structure, **not** the correctness of every word, number, formula, option, or diagram. The run flagged 71 questions for review; unflagged questions are not certified. Ten questions contained detected structured tables. Twenty-five questions had an automatically attached primary figure crop and none had an automatically attached additional figure; missed/vector/option figures may still require manual work.

Selected source comparisons included Q44's distinct `g/√3` and fraction bar, Q45 and Q56's simple fractions, Q52's superscript, Q55's cross-column table, Q80's three simple table fractions, and Q94/Q150's header/body tables split across pages. These examples have automated regression assertions; they do not establish universal accuracy. The local parser still flattens or misreads some complex expressions, including at least Q16 option D. Review **every** question, source region, bilingual passage, table and figure before use. The sample contains text-layer material; a scanned Hindi paper was not tested end to end and needs installed Tesseract plus the relevant language data or separately consented cloud OCR.

## Tests and actual external usage

- `manage.py test` passed **223 tests**, including three Chromium workflows and the optional local sample regression, on Python 3.11 with the supplied PDF path configured. Tests include synthetic/escaped tables, source permissions, teacher/student authorization, suggestion consent, model and response validation, human decision/staleness, quotas, deduplication, concurrent test start during a slow mocked request, and confirmed-key-only grading. The AI/active-test and existing private-practice concurrency tests also passed five repeated runs.
- Chromium exercised responsive student and teacher pages, table editing, original source excerpts, text-only consent and a bounded batch with a **mock** AI provider, then private CBT and results. Separate browser workflows checked the existing PDF/bank/report flow and collected production-style static assets. No actual AI output or payment was used.
- `manage.py check`, `makemigrations --check --dry-run`, JavaScript syntax checks and `git diff --check` passed. Migrations were applied to the local development database; deployment databases still require their own backed-up migration procedure.
- **Actual provider requests during this upgrade: zero** to Mathpix, Gemini and Razorpay. Therefore this session incurred no provider-request charges. Mock HTTP counts and token/cost values in tests are synthetic, not bills. Provider model/vision/schema compatibility, real cloud OCR quality, current pricing/retention and real Razorpay test checkout/webhook behavior remain unverified.

## Lower-cost extraction follow-up — 2026-11-03

- Added a separately configured and consent-gated **Gemini-only** mode. PyMuPDF prepares local text and embedded-image candidates; Gemini receives page previews and text, with no Mathpix request or setup fee. Both the previous local and Mathpix + Gemini modes remain available. Each Gemini question is flagged, no answer is automatically confirmed, and missing scanned/vector figures may need manual cropping.
- The optional mode is off by default. Its model, thinking level, output limit and per-job pacing are separate from the Mathpix pipeline. Pacing is not a cross-worker rate limit and the app does not impose a hard spending cap. Review logs show token counts only when returned by Gemini; the provider's bill is authoritative. Google free-tier data-use terms and quotas must be checked before use.
- On the supplied 26-page paper, **local preparation only** found 1,838 text blocks and 76 embedded-image candidates; candidates include possible logos and are not verified figures. The independent local parser still identified 180 labels. If the new cloud mode were explicitly consented, the paper would use 13 Gemini generation requests with 50 page previews including repeated context. No pages were sent and no Gemini transcription or real cost was measured.
- The complete suite passed **237/237** on Python 3.11, including the original-PDF local regression, four Chromium workflows and the new mocked Gemini-only consent/private-practice flow. The new tests also check disabled/missing consent, student plan permission, private storage, bounded model configuration, optional pacing, failure handling, and token reporting. **Real Mathpix, Gemini and Razorpay requests: zero.** Accuracy and the actual price of a live run remain unverified.

## Remaining operational work

1. Obtain current provider terms/prices and operator credentials in a secret manager, configure explicit model IDs, and only with the owner's authorization run a small representative non-sensitive live test; compare results and cost with manual ground truth. Metadata capability checks cannot guarantee output quality or multimodal support.
2. Review all extracted content and image associations. Local OCR and geometry are heuristics; some diagrams, vector art, equations, borderless tables and content across page breaks may be missed. Correct or exclude items before publishing or taking a test.
3. Configure HTTPS, backups, private storage, production database, rate limits, logs, a Celery worker and beat (or cron for expiry), and a real deployment security review. Existing teacher-bank image edits can affect institutional exams/history; review downstream use before changing them. A private bank snapshot can outlive a deleted source PDF.
4. PDF report math is currently text rather than typeset, and Hindi searchable/copyable text may have ligature issues. Accessibility and scanned Hindi OCR need independent validation. SQLite lock contention can return a safe retryable 503; use an appropriately configured production database.
