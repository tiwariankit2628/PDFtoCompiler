# TestForge CBT — Commercial-Grade Online Examination Platform

[![Django](https://img.shields.io/badge/Django-5.2%20LTS-092E20?logo=django)](https://www.djangoproject.com/)
[![Python](https://img.shields.io/badge/Python-3.10%E2%80%933.14-3776AB?logo=python)](https://www.python.org/)
[![Bootstrap](https://img.shields.io/badge/Bootstrap-5.3-7952B3?logo=bootstrap)](https://getbootstrap.com/)
[![License](https://img.shields.io/badge/License-Proprietary-blue.svg)]()

A high-performance, commercial-grade Computer-Based Testing (CBT) and online examination platform designed for competitive examinations (UPSC, SSC, Banking), engineering and medical entrance tests (JEE, NEET, GATE), school/university term exams, coaching institutes, and technical/coding assessments.

---

## Student PDF practice & duration

A signed-in student can open **My PDF Practice → Create from PDF**, upload a PDF, review or edit extracted questions and images, add missing questions, and include/exclude items. The settings page requires a whole-minute duration and previews the included question count, total possible marks, and duration. Presets are 15, 30, 45, 60, 90 and 120 minutes; custom values default to **5–360 minutes**. Set `PRACTICE_MIN_DURATION_MINUTES` and `PRACTICE_MAX_DURATION_MINUTES` to change the range (`1 <= min <= max <= 10080`). Uploads default to a maximum of 25 MB and `PRACTICE_MAX_PDF_PAGES=100`.

Students can explicitly delete a finished upload together with its original test and attempts; deletion is unavailable while extraction is running. Questions and image files still referenced by separately created bank tests are retained to preserve those tests and their history. Django admin blocks deletion of accounts or categories containing private practice data, and disables their bulk-delete action, to avoid exposing or accidentally cascading private material. Backups and any data previously sent to opted-in providers have their own retention policies.

A single-answer key can be entered per question or pasted as `1: A, 2: B`. Optional AI may suggest a choice, but a suggestion is **never automatically confirmed or used for grading**. Questions without a key are displayed as **ungraded**, not incorrect; the private score covers keyed questions only. Question and option images may be cropped from the source or uploaded, and unassigned cloud image candidates can be reviewed in the image pool. Always compare the text, formulas, options, keys and figures against the original: both local and cloud extraction can miss or misread content. PDF practice currently supports single-answer MCQs only.

Each private test stores its chosen duration. On start, the server stores one absolute expiry; reloads, resuming and reconnecting do not reset it. The database prevents two simultaneous active private attempts for the same student and test; an explicit new attempt after submission uses the same test duration and consumes an attempt from the student's plan. Selecting an answer is autosaved, and manual submission also saves the last visible answer atomically. Private tests do not consume the free trial, do not enter public catalog/leaderboards or institutional results, and are excluded from teacher/admin management views. Existing institutional exam duration settings remain unchanged.

### Auto-submit operations

The CBT server refuses answer changes after the stored expiry and finalizes overdue attempts on session, heartbeat, result, start, or practice-list requests. **For offline auto-submission when the student has closed their browser, run a worker and Celery beat**, with a shared database, private storage and Redis broker, for example:

```sh
# Set CELERY_ALWAYS_EAGER=False and CELERY_BROKER_URL appropriately first.
celery -A config worker -l INFO
# Run as a separate supervised service:
celery -A config beat -l INFO
```

Beat schedules `expire_private_practice_attempts` every 30 seconds; finalization may be delayed by scheduling or broker/worker availability, but expired answers remain locked. Monitor both services. As an alternative or recovery mechanism, schedule `python manage.py expire_practice_attempts` with cron. With development `CELERY_ALWAYS_EAGER=True` and no worker/beat or cron, offline attempts finalize on the next request rather than in the background. The new periodic sweep covers **student private practice only**, not institutional exams.

### Private files and optional providers

Private PDFs and figures are stored under `PRIVATE_PRACTICE_ROOT` (default `private_practice/`), separate from `MEDIA_ROOT` and `STATIC_ROOT`, and served only through owner-authorized views. New files use restrictive filesystem permissions; configure a protected persistent volume, access-controlled backups, and a web server that does **not** serve or symlink that directory. This is application-level isolation, not encryption against administrators with direct database/filesystem access. Deployments must also address authentication, HTTPS, quotas, abuse controls and user-data retention.

Local extraction is the default and does not send a student's PDF to Mathpix or Gemini. If the server enables and configures cloud extraction, a student may explicitly choose it and consent for that upload. The PDF is then sent to Mathpix and OCR/page images to Google Gemini, with provider charges and retention policies; deletion is requested from Mathpix but caches/metadata may remain. No real provider calls are made by the test suite. See the cloud setup section below, and verify current terms and pricing before use.

### Results, bank and data protection

- **Practice history** lists the student's attempts. A finished attempt has a scorecard, question-wise selections, the confirmed key and explanation where available, time, subject/topic/difficulty breakdowns, and comparison with other attempts. Scores and percentage use *confirmed-key marks* as the denominator. Accuracy uses only answered questions with confirmed keys. Questions without a key remain ungraded; they are not incorrect. A practice pass/fail outcome is shown only when a target is configured and every question has a confirmed key. The key is *student-confirmed*, not independently certified by the platform.
- Question time is an estimate based on browser save/heartbeat events; disconnects, a closed browser or time after the last save may not be recorded. Total time is measured separately from the server start and submit/expiry timestamps. Institutional examinations retain their existing behavior.
- **My Question Bank** is private to the owner. Included, approved PDF items are added when a private test is created; students can search/filter by subject, topic, chapter and difficulty, edit or remove an item and create a new test from selected items. Each bank edit uses copy-on-write if an existing test refers to the item, so existing question text, options, images, marks and attempt results remain unchanged. Removing an item from the bank retains its historical version where a test needs it. Deleting the original paper/test is explicitly destructive for its own attempts, but independent bank tests retain their referenced questions and images. Operators must account for these retained versions in their retention and erasure procedures.
- All result, report, source-paper and bank views require the owner. To avoid disclosing keys during a reattempt, these answer-bearing areas are temporarily blocked while **any** private test for that student is active. Other students, teachers and application-admin views cannot access private material. These controls do not protect against a server/database operator with direct access, or the student's own copy of the PDF or previously downloaded report.
- Downloadable reports include the scorecard, breakdowns, question-wise text, selected/confirmed answers, explanations, images and recorded times. PyMuPDF shapes English/Hindi using the embedded Noto Sans Devanagari font (SIL Open Font License, included in `static/fonts/`). Some complex Devanagari ligatures can render correctly while copy/search text extraction is imperfect; verify accessibility requirements for your use case. LaTeX is included as text in the report rather than typeset math, and missing source images are noted. Browser assets and their licences are in `static/vendor/`.

### Practice plans and Razorpay test mode

The migration creates a single default **Free practice** plan: 3 PDF uploads, 3 AI suggestion requests, 5 test creations and 10 new attempts per calendar month, plus 3 attempts per test. PDF reports, the bank and the cloud-extraction permission are enabled by default (cloud still needs configuration and per-upload consent). A Django superuser can edit limits, feature switches, availability and pricing in **Django admin → Practice plans** without code changes. Blank limits mean unlimited; `0` means none. Usage is counted in the `Asia/Kolkata` calendar month, includes both PDF- and bank-created tests, and is not refunded by deleting data. Resuming an existing attempt does not consume another attempt. The stricter of a test's own and the plan's per-test limits applies. Existing institutional/free-trial rules are separate. No paid plan or price is silently seeded; publish one only after deciding its terms.

Paid plans can be **one-time prepaid access** for a configured number of days, or **recurring** access tied to a Razorpay plan. A recurring plan's Razorpay test plan ID, currency, price in paise, period and interval must match the configured plan; the maximum cycle count is disclosed to the student. A student can request cancellation at cycle end, and verified paid access remains through its paid period. Overlapping plans do not stack; an earlier recurring subscription keeps renewing until cancelled. Same-plan duplicate purchases are blocked while access is active.

Set the following only in the server's secret manager or private environment, **not in Git or chat**:

```text
RAZORPAY_KEY_ID=<test key beginning rzp_test_>
RAZORPAY_KEY_SECRET=<test key secret>
RAZORPAY_WEBHOOK_SECRET=<separate test webhook secret>
```

The integration deliberately rejects missing, placeholder and `rzp_live_` credentials. Configure a dedicated HTTPS **test-mode** webhook at `/payments/razorpay/webhook/` for `payment.captured`, `order.paid`, `payment.failed`, `subscription.charged`, `subscription.cancelled`, `subscription.completed`, `subscription.halted`, and `refund.processed`. Test keys must correspond to the account that signs those webhooks. The public key ID alone is sent to Checkout. A browser callback is insufficient: the server verifies the SDK signature and fetches the captured payment, or verifies the signed raw webhook, then checks provider IDs, amount, currency and the paid subscription period. Payment records, webhook idempotency and entitlement creation are transactional. Full refunds revoke applicable access; partial refunds are recorded. For a recurring plan, a refund of an older paid period does not revoke a later separately paid period.

If a provider creation response is lost, the checkout is left **initiating**, no access is granted, and automatic retries are blocked to avoid accidentally creating another subscription. Investigate in the Razorpay *test* dashboard using the local checkout UUID (order receipt or subscription notes). An operator can link a matching provider record without activating access:

```sh
python manage.py reconcile_practice_checkout <checkout-uuid> <order-or-subscription-id>
```

The command verifies the provider record and frozen terms. It does **not** grant access; replay the original signed webhook or complete the normal server-verified checkout. Monitor failed webhooks and unresolved records, and never assume that a network timeout means the provider did nothing.

**Testing and deployment status:** automated billing tests exercise real HMAC/signature logic with mocked Razorpay API responses, including retries, expiry, recurring renewal and refunds. They do **not** perform an actual Razorpay sandbox transaction or prove provider compatibility, cancellation timing, refund behavior or webhook delivery in a merchant account. A real test-mode checkout, webhook replay and operations/security review are required before use; live mode is intentionally disabled. Configure `ALLOWED_HOSTS`, exact `CSRF_TRUSTED_ORIGINS`, HTTPS and, only behind a trusted proxy that strips client-supplied forwarding headers, `TRUST_PROXY_HTTPS=True`. Use PostgreSQL and durable protected storage in production; add rate limiting, monitoring, reconciliation, backup and retention policies. Preview/development settings are not production-ready.

Run the suite with `python manage.py test`; no real payment or OCR-provider calls are made by the tests. The real-browser smoke test is optional because it requires Node 20+ and a compatible Chromium binary:

```sh
(cd tests/browser && npm ci)
RUN_BROWSER_E2E=1 CHROMIUM_PATH=/path/to/chromium python manage.py test apps.questions.test_browser_practice apps.core.test_static_browser
```

The practice browser test starts an isolated test server/database, generates a synthetic PDF with images, and uses temporary private storage. It checks registration, upload/review, autosave/refresh, CBT, results, report, reattempt/privacy, history, bank, quotas UI and the unconfigured payment state without contacting providers. The static/UI browser test collects production assets into a temporary directory and uses WhiteNoise (not Django's development static handler) to check public pages, all four role dashboards, mobile menus, exam actions, KaTeX, Chart.js and the browser console at desktop and mobile widths. Both delete temporary test data afterward. Some container-only Chromium distributions also require system libraries, `LD_LIBRARY_PATH`, and `CHROMIUM_SINGLE_PROCESS=1`.

On PowerShell, set the environment variables before the same `python manage.py test ...` command: `$env:RUN_BROWSER_E2E="1"` and `$env:CHROMIUM_PATH="C:\path\to\chrome.exe"`.

---

## 1. System Architecture

The project is structured according to Django enterprise best practices with clean modular separation:

```
TESTgen/
├── config/                  # Core project settings, WSGI, ASGI, Celery application
│   ├── settings.py          # Dual-mode database (PostgreSQL/SQLite), WhiteNoise, auth
│   ├── urls.py              # Root URL routing (core, accounts, exams)
│   ├── celery.py            # Celery task broker configuration
│   ├── wsgi.py
│   └── asgi.py
├── apps/                    # Modular Django applications
│   ├── accounts/            # Custom User model (4 roles), CRUD views, management forms
│   │   ├── forms.py         # Registration, profile, student/teacher/admin management forms
│   │   ├── models.py        # Custom User model with permitted categories & freemium tracking
│   │   ├── views.py         # Login, register, profile, password reset flow
│   │   ├── views_management.py # Student, Teacher, and Administrator CRUD controllers
│   │   └── urls.py
│   ├── core/                # Landing page, public routes, role-based dashboard router, audit logs
│   │   ├── models.py        # AuditLog governance model
│   │   ├── permissions.py   # Strict role-based permission mixins
│   │   ├── utils.py         # Audit logging helper
│   │   ├── views.py         # Super Admin, Admin, Teacher, and Student Dashboards
│   │   ├── views_management.py # Audit Log exploration view
│   │   └── urls.py
│   ├── exams/               # Categories, examinations, candidate attempt models, Category CRUD
│   │   ├── models.py        # Category (with safe delete protection), Exam, ExamAttempt
│   │   ├── forms.py         # Dynamic CategoryForm with slug validation
│   │   ├── views.py         # Category list, create, edit, safe-delete, toggle status
│   │   └── urls.py
│   ├── questions/           # PDF compiler, OCR extraction, Question Bank (Phase 3-4)
│   └── payments/            # Freemium rules, Razorpay integration, invoices (Phase 7)
├── templates/               # Reusable Bootstrap 5 templates
│   ├── base.html            # Global layout, responsive navbar, notification toast, footer
│   ├── dashboard_base.html  # Unified dashboard shell with role-aware sidebar navigation
│   ├── core/                # Dashboards (super_admin, admin, teacher, student), audit_log_list
│   ├── exams/               # category_list.html, category_form.html
│   └── accounts/            # login, register, profile, management (student, teacher, admin)
├── static/                  # Static assets (CSS, JavaScript, images)
│   ├── css/style.css        # Custom CBT SaaS stylesheet
│   └── js/main.js           # Interactive notifications and UI components
├── staticfiles/             # Collected production static assets (WhiteNoise)
├── media/                   # User avatars and uploaded examination question diagrams
├── manage.py
├── requirements.txt         # Pinned production and development dependencies
├── .env                     # Local active environment variables
├── .env.example             # Template environment configuration
└── README.md
```

---

## 2. Environment Requirements & Checks

| Service / Dependency | Use | Configuration |
| :--- | :--- | :--- |
| **Python** | Runtime | Django 5.2 supports Python 3.10–3.14; Python 3.14 is recommended for a new installation. Check `python --version` locally. |
| **Django** | Web framework | Pinned in `requirements.txt`. |
| **PostgreSQL** | Production database | SQLite is the development default; set `DB_ENGINE=postgres` and the database settings for PostgreSQL. |
| **Tesseract OCR** | Scanned PDFs | Install the executable and required language packs separately; configure `TESSERACT_CMD` if needed. |
| **Redis / Celery** | Background work | A shared broker and worker/beat are needed for background tasks; development defaults to eager execution. |
| **Razorpay** | Test-mode practice billing | Configure test credentials server-side. Automated tests mock provider APIs; a real test-mode transaction has not been verified. |

These are requirements, not a live detection report for your computer.

---

## 3. User Roles & Authentication

The platform enforces strict role-based access control with four distinct roles:

1. **Super Admin**: Platform-wide governance, manage administrators, audit trails, category oversight.
2. **Administrator**: Student management, teacher onboarding, category creation, operational reports.
3. **Teacher / Content Manager**: Scoped strictly to permitted categories and self-authored tests.
4. **Student**: Candidate practice portal, performance summary, attempt history, free trial tracking.

### Optional Local Demo Accounts

`python manage.py seed_admin` creates these accounts if they do not exist. The passwords are public and predictable: **use them only in an isolated local development database, never in production**:

| Role | Username | Password | Email |
| :--- | :--- | :--- | :--- |
| **Super Admin** | `admin` | `admin12345` | `admin@cbtplatform.local` |
| **Teacher** | `teacher` | `teacher12345` | `teacher@cbtplatform.local` |
| **Student** | `student` | `student12345` | `student@cbtplatform.local` |

---

## 4. Setup & Execution Instructions

For a new Windows installation (PowerShell), from the project folder:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

On macOS/Linux, create the environment with `python3.14 -m venv .venv`, activate it with `source .venv/bin/activate`, and run the same `pip`, `migrate` and `runserver` commands. If needed, copy `.env.example` to `.env` **only when `.env` does not already exist**. Use `DEBUG=True` for local development; do not use development secrets or debug mode in production. Visit `http://127.0.0.1:8000/`. Install a supported Python version and the project requirements first; Python 3.14 itself has not been exercised by the current automated test environment.

To update an existing checkout, back up any local work and data, check `git status`, then fetch and fast-forward the intended branch. Do not overwrite an existing `.env`, database, media, or private files:

```sh
git fetch origin
git switch <your-branch>
git pull --ff-only origin <your-branch>
python -m pip install -r requirements.txt
python manage.py migrate
```

`python manage.py seed_categories` and `python manage.py seed_admin` are optional demo-data commands, not required for styling or migration; avoid demo credentials outside an isolated local database. Run `python manage.py test` for the automated suite.

### Static files and troubleshooting

Bootstrap, Bootstrap Icons, KaTeX and Chart.js are bundled under `static/vendor/` along with their referenced fonts and source maps. Development uses Django's `staticfiles` finders; it does not need a CDN or `collectstatic`. `STATIC_URL` is `/static/`, and `STATICFILES_DIRS` includes the project `static/` directory. Check, for example:

```sh
python manage.py findstatic vendor/bootstrap/bootstrap.min.css --verbosity 2
```

In a production environment configured with `DEBUG=False`, run **`python manage.py collectstatic --noinput` after every asset update**, then deploy/restart the application with the resulting `STATIC_ROOT` available to WhiteNoise. Production uses compressed manifest storage, which fingerprints filenames and rewrites references to fonts and source maps. Do not add an upstream pre-collection `integrity` hash to these same-origin bundled files: it does not match the rewritten bytes and the browser will block the asset. If assets are served by a separate CDN or proxy, configure it to serve the collected manifest and files consistently. Production HTTPS, secrets, storage and proxy settings still require proper deployment configuration; `runserver --insecure` is not a production static server.

If a local page still looks unstyled, confirm the checkout/branch and `DEBUG` value, restart `runserver`, and hard-refresh the browser. In the browser's Network and Console tabs, check that `/static/vendor/bootstrap/bootstrap.min.css`, the Bootstrap JS bundle, icon font and `css/style.css` load successfully; with `DEBUG=False` their URLs contain fingerprinted names. A 404 usually means the files or collected output are missing or the server is serving the wrong directory. A browser integrity error indicates stale/mismatched markup or cached assets; redeploy the matching templates and collected assets, restart the server, and clear relevant browser/proxy caches. Do not delete user databases, uploads or private files as a troubleshooting step.
---

## PDF extraction: setup, privacy and review

**The default is local extraction.** It does not send the PDF to Mathpix or Google. Digital layout extraction is heuristic; scanned pages require a separately installed Tesseract executable and appropriate English/Hindi language packs. The local automatic parser may miss equations, diagrams, option images, and difficult layouts. Manual cropping/attachment is available regardless of extraction mode.

The optional **Mathpix + Gemini** pipeline is disabled by default. To enable it, an administrator must configure the server's private `.env` or secret manager (never put real credentials in Git or send them in chat):

```dotenv
PDF_CLOUD_ENABLED=True
MATHPIX_APP_ID=<your-server-side-app-id>
MATHPIX_APP_KEY=<your-server-side-app-key>
GEMINI_API_KEY=<your-server-side-api-key>
GEMINI_MODEL=<model-id-available-in-your-account>
PDF_CLOUD_MAX_PAGES=50
PDF_CLOUD_POLL_SECONDS=3
PDF_CLOUD_TIMEOUT_SECONDS=900
PDF_CLOUD_THINKING_LEVEL=
PDF_CLOUD_MAX_OUTPUT_TOKENS=32768
```

1. Install the project requirements and apply migrations: `python manage.py migrate`.
2. For background processing, run a Redis broker and a Celery worker, set `CELERY_ALWAYS_EAGER=False`, configure `CELERY_BROKER_URL`, and start `celery -A config worker --loglevel=INFO`. Use PostgreSQL for production concurrency; SQLite is intended for local development. Web and worker processes need the same database, private settings and shared media storage.
3. With `CELERY_ALWAYS_EAGER=True` (the development default), cloud extraction runs **synchronously inside the upload request** and can take several minutes or exceed web-server timeouts. Do not use that setting for long-running production jobs.
4. In **Upload Question Paper**, choose **Mathpix + Gemini** and explicitly consent for that specific upload. Merely configuring keys does not send existing PDFs. A 25 MB upload limit and the configurable cloud page limit apply. Refresh the review page while a background task is pending or running; examine its log for errors. If a worker dies, check the broker/worker and the paper's status. To retry, upload a fresh copy and consent again; it may be billed again.

Cloud processing uploads the full PDF to Mathpix. It sends the OCR text, normalized layout regions and rendered page images to Google's Gemini API, in two-primary-page batches with neighboring-page context. Images used in Gemini calls are compressed previews; figures saved in the app are cropped from the original PDF with PyMuPDF, not generated by AI. An uploaded paper is never sent without the cloud option and explicit per-upload consent. The app requests deletion of the Mathpix document after OCR, including on processing errors when the provider ID is known. Deletion can fail, a timed-out submission can have an unknown ID, or a killed worker may not run cleanup. A known Mathpix document reference is recorded on the paper as soon as the provider returns it; check the paper log/reference and contact Mathpix if needed. CDN caches and minimal billing/audit metadata may remain. Review [Mathpix retention and deletion](https://docs.mathpix.com/concepts/data-retention) and [Google's Gemini API terms and privacy](https://ai.google.dev/gemini-api/terms) for your account and region before uploading sensitive material. The PDF, review data and crops are also stored in the app's media/database until managed by the operator; this feature does not implement a complete retention policy.

Before a Mathpix upload, the worker checks that the explicitly configured Gemini model advertises `generateContent`; this metadata request contains no document. It cannot prove vision/schema compatibility or accuracy. Both services can charge your accounts. As listed by Mathpix on 2026-11-02, PDF OCR was $0.005/page for the first tier, with a one-time $19.99 setup fee; taxes and other fees may apply. Gemini is billed separately, and overlapping page context increases image/input usage. Pricing and terms can change: check [Mathpix pricing](https://mathpix.com/pricing/api) and [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing) before use. A provider failure stops the run and is reported; there is no silent local fallback and submission is not automatically retried.

### Optional AI answer suggestions

This is separate from PDF transcription and is **disabled by default**. Configure an available Gemini model ID and API key privately on the server; do not assume a particular model is available or put credentials in Git or chat:

```dotenv
AI_ANSWER_ENABLED=True
GEMINI_API_KEY=<your-server-side-api-key>
AI_ANSWER_MODEL=<model-id-available-in-your-account>
AI_ANSWER_MIN_CONFIDENCE=0.70
AI_ANSWER_THINKING_LEVEL=
AI_ANSWER_MAX_OUTPUT_TOKENS=32768
# Optional estimate rates; leave blank if unknown. Provider invoices are authoritative.
AI_INPUT_USD_PER_MILLION=
AI_OUTPUT_USD_PER_MILLION=
```

The optional thinking level may be set to `low`, `medium`, or `high` only when the chosen model supports it; otherwise leave it blank for the provider default. The maximum output budget (512–65536) includes reasoning tokens on some models; increasing it or choosing deeper thinking can increase cost and latency. The operator should choose a currently available, high-capability multimodal/reasoning model that supports `generateContent`, image input and structured output, then benchmark accuracy on a separately authorized representative set. Model capability, access, retention and price change; the app does not hardcode a purported “best” ID. Its metadata check verifies `generateContent`, not necessarily vision/schema support or quality. No model has been live-tested by this project upgrade.

- In a student or teacher staging question, select **AI answer review**. The checkbox is required for each request: the question, options and structured tables are sent to Gemini. A separate checkbox permits bounded original-question excerpts and attached images; the full PDF is not sent by this answer feature. A source excerpt can include adjacent content, so review it and the provider's terms first. Original full-PDF Mathpix extraction has its own separate per-upload consent.
- The reviewer sees the suggested choice, explanation, confidence and uncertainty. Low-confidence choices, and choices requiring unavailable visual input, are withheld. Even a confident suggestion can be wrong. A person may accept or edit the choice and explanation only after explicitly confirming personal review, or reject it. Only that human decision can set a key. Changes to question text, options, tables or recorded image associations make an earlier suggestion stale.
- On the staging review page, **AI suggestions for several questions** queues up to five explicitly selected items in the browser. These are **separate sequential provider requests**, not a provider-side bulk API; each has independent quota, potential charges and review. Keep the page open. Closing it or a timeout cannot guarantee cancellation or absence of charges. An unchanged processing/suggested request is reused; a reviewed/rejected or changed item can be requested again.
- Student plans control monthly AI requests (blank is unlimited, `0` disables); the default free plan permits three. Teacher requests are not subject to student plan quotas, so operators should apply appropriate institutional abuse and budget controls. Requests reserve quota before processing; failed or timed-out calls may still consume quota or incur charges. Usage, HTTP attempts, provider-reported token counts and an optional operator-supplied cost estimate appear in the review UI. Missing token/pricing data means **unknown**, not free; costs are estimates and may omit provider-specific billing components. The app never stores API secrets in question records.
- Transient model-metadata errors and a definite rate-limit response are retried once. Timeouts and ambiguous server errors are not automatically retried because they may have incurred charges. Model response content is validated and treated as untrusted; high model confidence does not prove correctness. All result/source/bank/answer-review views remain blocked during an active private attempt.

### Tables, mathematics and provenance

Digital extraction uses text geometry, table rulings and a conservative fraction/superscript reconstruction. It stores actual table cell grids with merged rows/columns and `[[table:1]]` markers. The cell editor supports edits and adding/removing rows; advanced JSON allows more complex spans. Staging, bank, CBT and result views render escaped responsive HTML tables and KaTeX math. Original-page region previews and crop/option-image controls help reviewers correct failures. Figures retain source, page and normalized rectangle where known; auto-association is heuristic and uncertain items must be reviewed. Manually uploaded figures have no original coordinates.

Extraction is not a general math/layout OCR system. A ruled fraction or single-character radical may be reconstructed, but complex equations, Type3 glyphs, borderless tables, vector drawings, scans, Hindi OCR and page/column continuation can still be wrong or omitted. Even unflagged items need checking against the original. Cloud-returned source coordinates are model suggestions, not independently verified; missing coordinates can be reviewed on the original page. Deleting the original paper removes its source PDF, even if separately retained bank snapshots survive. PDF reports render tables but currently include LaTeX as text rather than typeset formulas; Hindi copy/search and accessibility need further validation.

Local-only source-paper observations and test scope are in [the upgrade validation report](docs/upgrade-validation.md).

### Review workflow

- Compare all questions, English/Hindi text, formulas, tables, options and images with their source pages. The cloud model can hallucinate, omit or rearrange content; its structured JSON is validated but is not a guarantee of accuracy. Every cloud-derived question is flagged until a teacher reviews and clears it. Local extraction also reports some uncertainties, but unflagged local items still need review.
- Question source links include continuation pages when detected. Use **Add missing question** if a label is missing and the extraction log/numbering indicates a gap. Numbers, options and flags are editable. No answer key is guessed: import or enter the actual key separately, or leave it blank. Questions without an answer key are not ready for automatic scoring.
- The cloud pipeline matches candidate figure regions to question or option IDs, keeps ambiguous/unmatched candidates in **Image pool** (which may contain logos), and rejects shared or invalid matches. Region alignment checks are heuristic, not proof. A teacher can drag a crop rectangle over a source page, attach or replace image files, move images to a question or option, or return them to the pool. Multiple images and image-only options are supported in staging, the bank, exam preview/session and result review.
- Image changes re-flag staging questions. Approve only after review; flagged, incomplete or structurally invalid items are skipped by bulk approval. Approved staging images become read-only; use **Manage images** in the question bank for later corrections. Changes to bank questions can affect exams and historical displays. Duplicate linking is conservative when content, marks, keys or figures differ.
- Extraction never replaces an existing staging workspace. To run a fresh extraction, upload a new copy rather than overwriting teacher edits. Provider tests use synthetic PDFs and mocks; no real-provider accuracy or cost benchmark is implied.

**Deployment warning:** the development configuration serves `MEDIA_ROOT` directly when `DEBUG=True`, which can expose uploaded PDFs and image files by URL despite protected review endpoints. Do not use it for confidential papers. Configure access-controlled private storage and media delivery, secrets, HTTPS, retention/backup policies, and a full security review before production. The broader examination and payment features have not been established as production-ready.

---

## 5. Development Phases Roadmap

- [x] **Phase 1: Foundation & Authentication** (Completed)
  - Python 3.12 & Django 5.2.18 LTS configuration.
  - Multi-app modular architecture (`accounts`, `core`, `exams`, `questions`, `payments`).
  - Safe dual-mode database engine (PostgreSQL + SQLite dev fallback).
  - Custom User model with 4 distinct roles, target exam, verification token, freemium tracker.
  - Public portal: landing page with interactive CBT simulation card, about, contact, auth suite.
- [x] **Phase 2: User Roles, Permissions & Professional Dashboards** (Completed)
  - Four distinct, server-side secured dashboards:
    - **Super Admin Dashboard** (`/dashboard/super-admin/`): Real-time metrics, user growth, recent registrations, security audit trail.
    - **Admin Dashboard** (`/dashboard/admin/`): Student/teacher/category management shortcuts, recent exam attempts.
    - **Teacher Dashboard** (`/dashboard/teacher/`): Scoped strictly to teacher's authored tests, student attempt counts, and assigned categories.
    - **Student Dashboard** (`/dashboard/student/`): Real attempt history, accuracy percentage, freemium eligibility badge, available tests.
  - **Dynamic Category Management** (`/exams/categories/`): Complete CRUD, display ordering, SEO meta, active toggle, and protected deletion validation.
  - **Student Candidate Management** (`/auth/students/`): Search, filter by active/inactive, add candidate, edit profile, toggle status, attempt history.
  - **Teacher / Faculty Management** (`/auth/teachers/`): Add teacher, assign permitted categories, view teacher-created tests, toggle status.
  - **Administrator Management** (`/auth/admins/`): Super-admin restricted CRUD for platform admins with protection against self-deletion.
  - **Audit Logs** (`/management/audit-logs/`): Platform-wide governance log with search, filter, IP addresses, and timestamps.
  - Full automated test suite (27 tests passing, 0 system check issues).
- [x] **Phase 3: PDF Upload & Question Extraction System (PyMuPDF + OCR)** (Completed)
  - **Secure PDF Upload** (`/questions/papers/upload/`): 25MB file validation, MIME check, and PyMuPDF stream integrity verification.
  - **Automated Extraction Pipeline** (`apps/questions/extractor.py`, `apps/questions/pdf_layout.py`):
    - Digital papers with explicit `Q1` / `Question 1` / `प्रश्न 1` labels are read in page and column order; questions can continue into the next column or page.
    - Embedded figures are matched by position and rendered from the original PDF. Repeated footer logos and page watermarks are excluded where identifiable.
    - A basic text/Tesseract fallback supports other numbering styles and scanned pages, but does not automatically assign diagrams and flags its results for review. Install Tesseract and the required language data for OCR; unavailable OCR is reported in the review page and log.
    - Local extraction is heuristic; mathematical notation, tables and image associations may need manual correction. The optional consent-gated Mathpix + Gemini pipeline and the manual image editor support multiple question and option images.
    - No answer key is guessed. Review every question against the source PDF, including items without an automatic flag. Upload a fresh copy to apply extraction changes to a previously processed paper; existing review items are not automatically reprocessed.
  - **Interactive Review Workspace** (`/questions/papers/<pk>/review/`):
    - Staging review table with filter tabs (`all`, `pending`, `flagged`, `approved`) and a source-page link for each question.
    - Editable question editor (`/questions/extracted/<pk>/edit/`): modify text, options, marks, diagrams.
    - Automatic flags for incomplete questions and some potentially unreliable math, tables, or diagrams; flags do not detect every error.
    - Flagged and structurally incomplete questions cannot be approved until corrected; bulk approval skips them.
    - Conservative duplicate linking only when the question, options, answer, marks and other content match; image-bearing questions are not silently linked.
    - Quick answer-key importer supporting standard formats (`1: A, 2: B` or newline delimited).
  - **Access Control** (development media serving is not private; see the deployment warning above):
    - Scoped access: Admins see all papers; Teachers see authored/assigned category papers; Students blocked (403 Forbidden).
    - Role-checked PDF streaming endpoint (`/questions/papers/<pk>/pdf/`); direct development media URLs can bypass it and must not be used for confidential data.
  - **Question Bank** (`/questions/bank/`): Searchable, filterable question catalog with category and difficulty tags.
  - Full automated test suite (45 tests passing, 0 system check issues).
- [x] **Phase 4: Question Bank & Examination Creation** (Completed)
  - **Comprehensive Question Bank Management** (`/questions/bank/`):
    - Manual question authoring workspace (`/questions/bank/create/`) with KaTeX mathematical formulas rendering, LaTeX syntax support, Hindi/Devanagari Unicode support, and rich explanations.
    - Standard MCQ options (A, B, C, D) configuration with instantaneous answer key marking and minimum 2 options validation.
    - Category scoping, subject, topic, chapter taxonomy, and difficulty level grading (Easy, Medium, Hard).
    - Custom positive and negative marking configuration per question.
    - Question editing (`/questions/bank/<pk>/edit/`) with historical attempt warnings.
    - Protected question deletion (`/questions/bank/<pk>/delete/`) preventing deletion if linked to completed candidate exams.
    - Search, filtering by category, subject, difficulty, language, and paginated catalog.
    - Real-time teacher/admin question preview modal (`/questions/bank/<pk>/preview/`) rendering formulas, diagrams, and options.
  - **Professional CBT Examination Builder** (`/exams/`):
    - Multi-section exam creation (`/exams/create/`): title, description, instructions, category scoping, subject, topics, target exam.
    - Engine parameters: total duration (minutes), passing percentage, default positive/negative marks, attempt limits.
    - Anti-cheating & security toggles: question shuffling, option randomization, immediate result disclosure, and answer key review permissions.
    - Flexible access models: Free vs. Premium assessment gating.
    - Status management: `draft`, `published`, `scheduled`, `archived` with scheduling start/end window validation.
  - **Interactive Question Builder** (`/exams/<pk>/questions/`):
    - Visual question management dashboard showing assigned questions, sections, and individual marks.
    - Manual bulk question picker (`/exams/<pk>/questions/add-bulk/`) with category, subject, and difficulty filters.
    - Dynamic random question sampler (`/exams/<pk>/questions/add-random/`) to randomly draw $N$ questions matching subject and difficulty constraints.
    - Dynamic recalculation of total questions count and total exam marks.
    - Full examination preview (`/exams/<pk>/preview/`) rendering test instructions, question palette, and KaTeX equations before publishing.
  - **Exam Lifecycle & Governance**:
    - Publish validation: ensures examinations have at least 1 question and valid duration before going live.
    - Instant duplication engine (`/exams/<pk>/duplicate/`): clones complete exam settings, question sets, and custom scores.
    - Historical data protection: prevents deletion of examinations that have active candidate attempt records.
    - Server-side role scoping: Students blocked with HTTP 403; Teachers restricted strictly to permitted categories and own content.
  - Full automated test suite (55 tests passing across all apps).
- [x] **Phase 5: Professional CBT Examination Engine (Timer, Palette, Auto-Submit, Anti-Cheat)** (Completed)
  - **Pre-Exam Readiness & Instructions Portal** (`/exams/<slug>/instructions/`):
    - Detailed candidate instructions, syllabus summary, duration, marking scheme (+positive, -negative), passing benchmarks.
    - Interactive palette status guide with color legends.
    - Candidate compliance declaration checkbox and eligibility validation.
    - Smart Resume CTA: Automatically detects active `IN_PROGRESS` sessions and resumes seamlessly without creating duplicate attempts.
  - **Candidate CBT Examination Interface** (`/exams/session/<uuid>/`):
    - Distraction-free, responsive full-screen examination environment.
    - Dynamic single-question viewport with section-wise tab navigation.
    - LaTeX mathematical formulas rendering via KaTeX and responsive question diagram viewer.
    - MCQ options selection with instantaneous visual feedback.
    - Navigation actions: "Save & Next", "Clear Response", "Mark for Review & Next", "Previous", and direct Palette jumping.
  - **Navigation Palette & Real-Time Status Engine**:
    - Complete 5-state NTA/TCS-standard question palette:
      - Gray: Not Visited
      - Red: Not Answered (visited but skipped)
      - Green: Answered
      - Purple: Marked for Review (without answer)
      - Purple with Green dot: Answered & Marked for Review
    - Real-time question counter badges (Answered, Unanswered, Marked, Not Visited).
  - **Server-Authoritative Timer & Timeout Engine**:
    - Server-stored `started_at` and `expires_at` timestamps.
    - 30-second periodic heartbeat synchronization with server clock.
    - Visual urgency alert: Timer transitions to animated red pulse when $< 5$ minutes remain.
    - Server-side deadline enforcement: overdue answers are rejected and attempts are finalized on a subsequent request. Private-practice offline finalization additionally requires the worker/beat or cron setup described above.
  - **Reliable Answer Persistence & Concurrency Safety**:
    - Real-time AJAX response autosave (`/exams/session/<uuid>/save-answer/`) with database row locking (`select_for_update`).
    - Browser refresh and network reconnection restore responses, review flags, and palette states that were successfully saved to the server.
    - Edit lock: Immediately rejects response edits after session submission or expiry.
  - **Commercial-Grade Examination Security**:
    - **Answer Protection:** Answer keys, correctness flags (`is_correct`), and solution explanations are strictly stripped from client HTML and JSON payloads.
    - **Candidate Isolation:** Server enforces strict ownership check (`HTTP 403 Forbidden` if candidate attempts to access another candidate's session).
    - **Anti-Cheat Monitoring:** Full-screen entry prompt, tab switch (`visibilitychange`) logging, window focus loss (`blur`) tracking, copy-paste/cut prevention, and right-click context menu restrictions.
    - Secure `ExamSecurityLog` records timestamps and violation details for institutional proctoring review.
  - **Atomic Final Submission & Score Evaluation**:
    - Modal confirmation with candidate response summary before final submit.
    - Atomic calculation of positive marks, negative penalties, net score, percentage, and pass/fail benchmark.
    - Post-submission screen (`/exams/session/<uuid>/completed/`): Immediate provisional score report or institutional evaluation notice based on test rules.
  - Full automated test suite (72 tests passing across all apps).
- [x] **Phase 6: Result Evaluation & Deep Performance Analytics** (Completed)
  - **Comprehensive Student Result Dashboard** (`/exams/results/<uuid:attempt_uuid>/`):
    - Real-time score breakdown: Total marks, marks obtained, percentage, pass/fail status, and accuracy percentage.
    - Question status counters: Correct, incorrect, and unattempted metrics.
    - Time analysis: Total time taken, average time per question, pacing comparison (fastest vs slowest questions).
    - Diagnostic subject and topic mastery cards with dynamic accuracy progress bars.
    - Identified Strengths (topics with $\ge 70\%$ accuracy) and Weak Areas (topics with $< 50\%$ accuracy) derived from real attempt data.
    - Interactive Chart.js visual charts: Subject-wise score vs total marks, and Difficulty-wise accuracy distribution.
    - Historical examination attempt comparison table.
    - Respects `exam.show_result_immediately` policy, concealing premature scorecards until officially published.
  - **Question-by-Question Solution & Answer Review System** (`/exams/results/<uuid:attempt_uuid>/review/`):
    - Detailed question review workspace displaying candidate's selected choice, correct answer, question marks, and marks awarded/deducted.
    - KaTeX mathematical equations and diagram visualization in both questions and explanations.
    - Complete step-by-step explanatory solutions for every question.
    - Interactive filter pills to view "All", "Correct", "Incorrect", or "Unattempted" questions.
    - Security barrier: Strictly enforces `exam.allow_review_answers` and result release schedule before exposing answer keys or solutions.
  - **Vector PDF Scorecard Generation Engine** (`/exams/results/<uuid:attempt_uuid>/pdf/`):
    - High-resolution, vector-based A4 official PDF scorecards generated dynamically using PyMuPDF (`pymupdf`).
    - Complete header branding, candidate identification details, KPI scorecards, subject breakdown table, and timestamped security verification token.
  - **Standard Competition Ranking & Percentile Engine**:
    - Automated cohort evaluation and ranking upon submission using standard competition ranking (1224).
    - Multi-tiered tie-breaking rule:
      1. Higher net score obtained
      2. Fewer incorrect answers (higher precision)
      3. Faster completion time (efficiency)
    - Percentile calculation formula: $\frac{\text{Candidates Scored Lower}}{\text{Total Candidates}} \times 100$, with single-candidate baseline handling.
    - Public / Candidate Leaderboard (`/exams/<slug:slug>/leaderboard/`) with cohort summary stats (highest score, average score, pass rate) and highlighted personal candidate standing.
  - **Institutional Teacher & Administrator Analytics Dashboard** (`/exams/<int:pk>/analytics/`):
    - Cohort performance metrics: Total attempts, pass count, pass rate, class average score, highest/lowest scores.
    - Score distribution histogram (ranges: $<30\%$, $30-50\%$, $50-70\%$, $70-90\%$, $>90\%$).
    - Question Difficulty & Discrimination matrix: Evaluates candidate accuracy per question, auto-classifies items as *Easy* ($\ge 70\%$), *Moderate* ($40-69\%$), or *Challenging* ($< 40\%$), and identifies top distractor choices.
    - Candidate comparison table with individual scores, accuracy, ranks, and completion dates.
    - Scoped strictly by role permissions (Teachers only access own or assigned-category exams; Admins access platform-wide).
  - **Institutional CSV Result Export** (`/exams/<int:pk>/export-results/`):
    - Instant CSV download for Teachers and Admins with candidate rank, name, roll/username, email, target exam, marks, percentage, accuracy, time taken, and pass/fail outcome.
  - **Data Privacy & Candidate Isolation**:
    - Server-side candidate ownership verification: Unauthorized candidates attempting to inspect another student's result or review are rejected with `HTTP 403 Forbidden`.
  - Full automated test suite (82 tests passing across all apps).
- [ ] **Phase 7: Student insights, private bank and test-mode billing** — implemented and covered by automated tests; a real Razorpay test-mode checkout and webhook still require merchant credentials and provider verification.
- [ ] **Phase 8: Security Testing, Performance Optimization & Cloud Deployment**
