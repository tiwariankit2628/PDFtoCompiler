# TestForge CBT — Commercial-Grade Online Examination Platform

[![Django](https://img.shields.io/badge/Django-5.2%20LTS-092E20?logo=django)](https://www.djangoproject.com/)
[![Python](https://img.shields.io/badge/Python-3.10%E2%80%933.14-3776AB?logo=python)](https://www.python.org/)
[![Bootstrap](https://img.shields.io/badge/Bootstrap-5.3-7952B3?logo=bootstrap)](https://getbootstrap.com/)
[![License](https://img.shields.io/badge/License-Proprietary-blue.svg)]()

A computer-based testing platform currently focused on JEE Main, JEE Advanced and NEET. Student accounts choose either the JEE or NEET track, and tests and questions have an explicit exam target.

---

## Commercial model and current status

| Use | First use | Additional access |
| --- | --- | --- |
| Student PDF extraction | First PDF free | ₹299 for 20 more PDFs |
| Student classroom | First test start free | ₹99 for 20 more test starts |
| Teacher classroom | First activation free | ₹299 for 20 more activations, each for one test for 24 hours |
| JEE series | Test 1 free | ₹299 unlocks tests 2–16 in the JEE series |
| NEET series | Test 1 free | ₹299 unlocks tests 2–16 in the NEET series |

The free PDF, classroom and series allowances are independent and one-time; unused paid credits currently do not expire. One teacher activation can serve multiple classes/codes during its 24-hour window, without a second teacher credit. A student can resume the same running attempt, but cannot retake the same classroom, series or private test, even using another classroom code. New classroom and series scores and the student's own responses are available after submission; key/explanation release is separately controlled. Series and classroom starts do not use each other's credits. A different PDF or bank snapshot is a different test identity.

**This is not ready for real-money use.** Razorpay is test-mode only and test credits default to disabled; real provider transactions and actual email delivery have not been tested. The JEE/NEET series slots are empty until an administrator supplies and reviews real content. See [commercial operations and safe migration](docs/commercial-operations.md) for verification email, isolated sandbox setup, releases, recovery and limitations.

## JEE and NEET scope and upgrade

- Students must choose JEE or NEET when registering. JEE students can access JEE Main, JEE Advanced and general JEE content; NEET students can access NEET content. The catalog, classroom codes, new test starts and private practice enforce the track server-side. Teachers select an exam target for new tests, questions and PDF uploads.
- A student can change track in their profile after finishing active tests. Earlier results remain available, and a live pre-upgrade attempt can be resumed with its original deadline even if its old classification is missing or different. These exceptions preserve existing work; they do not permit a new incompatible attempt.
- For an existing installation, **back up the database and uploaded/private files first**, then run `python manage.py migrate` and restart. Recognizable exact old labels are classified conservatively; ambiguous student accounts and content remain unclassified, not guessed or deleted. Students select their track in their profile; owners review and classify older papers/tests, and teachers review unclassified exams and bank questions before use. Past scores, answers, legacy labels and deadlines are retained. Django admin shows these target fields read-only; use the application profile, student management, paper review, exam editor or question editor for protected changes.
- Local PDF type/section detection is heuristic. Review the original images, question type, marking rules and answer key before confirming or creating a test. JEE Main numerical questions support signed decimal entry and checked numerical scoring; no answer is invented. Not every official marking scheme is implemented.

## Teacher classroom tests from PDFs

1. Sign in with a teacher profile and open **Classroom Tests → Create Test from PDF**. Select its JEE Main, JEE Advanced, general JEE or NEET target and upload a PDF (up to 25 MB). New uploads use local PyMuPDF/Tesseract extraction only; no PDF is sent to a provider and no paid API key is needed. Extraction currently runs in the upload request, so a large PDF may take time. Scanned pages require working local OCR/language data; if unavailable, the review log warns that content may be missing.
2. Compare the preview and original PDF. Edit or add questions, include/exclude them, and assign, upload or recrop images. For image-only choices in an original question crop, explicitly select the labels actually visible in the picture; the app does not invent option text. Check for printed answer keys inside crops as automatic detection is not exhaustive. Check each question type, key and scoring rules. Single-correct, multiple-correct and numerical questions can be included; unsupported subjective items must be edited or excluded. Missing or unconfirmed keys remain **ungraded**, not wrong.
3. Set a duration of 1–1,440 minutes and confirm your review to create a private snapshot and a join code. Later edits to the paper do not change that test or regrade past results; create another test for revised questions/keys. A code-only test is not listed in the public exam catalog. Teachers can also start separate classroom sessions from their own published tests.
4. Students sign in with a student profile and enter the code or open its link. They can join from different locations and devices; they do **not** need the same Wi-Fi. The application must actually be deployed at a reachable HTTPS address with correct host and CSRF settings. A local-only server or temporary preview is not a permanent internet deployment. There is no application-imposed participant cap, but server capacity, database, bandwidth and hosting limits still matter.
5. The teacher's session page shows distinct joined, started, in-progress, submitted, timed-out/ended and security-review counts plus participant statuses. Closing joining does not stop existing attempts; ending the test finalizes saved answers. Regenerating a code invalidates the old code. New sessions show scores and the student's own answers after submission; correct keys/explanations may be released separately per session. Migrated sessions retain their earlier release settings. Releasing keys while others are testing can disclose answers; scores and original images can also reveal answers. The optional three-warning page-switch rule ends an attempt on the fourth reported switch; these are signals for review, not proof of cheating.

Each student gets one server-side deadline from their actual start, unchanged by refresh or reconnect. Answers autosave; a visible retry control appears after a failed save, and final submission waits for queued saves and saves the visible answer atomically. A browser cannot send an answer after the deadline; disconnected or unsaved local changes may be lost. For unattended expiry processing, run the worker and beat or cron described below.

The leaderboard and CSV use one **best fully keyed completed attempt** per student, ordered by score, fewer incorrect answers, then faster completion; equal results share a competition rank. For historical sessions with existing multiple attempts, the participant row shows the latest attempt's status and the best completed score; new retakes are blocked. Status counts can therefore overlap. Partial-key, ungraded, active and security-terminated attempts are not ranked; the CSV labels these separately, keeps one row per joined student, and places ranked students first. A fully keyed, unanswered attempt is a recorded zero. Historical results use their saved grading decisions and are not silently regraded.

Classroom source PDFs and copied images use the protected `PRIVATE_PRACTICE_ROOT`, not public media URLs. Teacher paper tools are owner-only; students can fetch only images in their own attempt, and answer review obeys the session release setting. Session owners and application administrators can review classroom results; database/server operators can access underlying files, so this is not encryption. Use protected persistent storage, backups, HTTPS and production access controls.

## Student PDF practice & duration

A signed-in student can open **My PDF Practice → Create from PDF**, upload a PDF, review or edit extracted questions and images, add missing questions, and include/exclude items. The settings page requires a whole-minute duration and previews the included question count, total possible marks, and duration. Presets are 15, 30, 45, 60, 90 and 120 minutes; custom values default to **5–360 minutes**. Set `PRACTICE_MIN_DURATION_MINUTES` and `PRACTICE_MAX_DURATION_MINUTES` to change the range (`1 <= min <= max <= 10080`). Uploads default to a maximum of 25 MB and `PRACTICE_MAX_PDF_PAGES=100`.

Students can explicitly delete a finished upload together with its original test and attempts; deletion is unavailable while extraction is running. Questions and image files still referenced by separately created bank tests are retained to preserve those tests and their history. Application and Django-admin account deletion is guarded for accounts with assessment or billing records; Django admin also blocks category deletion when it contains private material and disables bulk deletion for both. These guards are not substitutes for backups. Backups and any data previously sent to opted-in providers have their own retention policies.

Enter checked answers individually or paste typed pairs, for example `1:1, 2:[1,3], 3:-2.5`: question 1 maps printed option 1 to A; question 2 accepts choices A and C; question 3 keeps the actual numerical value −2.5. Brackets are required for a multiple-correct set. Numerical keys and responses accept a sign, up to 12 integer digits and 6 decimal places, and a reviewer-selected non-negative tolerance (0 means exact). For multiple-correct questions choose exact-set scoring, or a fixed partial mark per correct choice for a proper subset, capped at the full mark; a selection containing a wrong option gets the configured negative mark. **Check the actual exam rules:** other schemes, including asymmetric numerical ranges or more complex JEE Advanced partial/negative marking, are not implemented and must not be represented as if they were.

Up to three JPEG/PNG key photos can be read locally for **single-choice suggestions only**; enter multiple and numerical keys manually. Image readings are untrusted even when they look clear: compare the original paper, set and printed choice order, edit the results, and explicitly confirm them. Missing, ambiguous or duplicated entries need review; the reader never infers missing answers from numbering. Photo reading uses locally installed English OCR data; if unavailable, typing and pasting still work. This applies to student practice, teacher classroom and ordinary teacher paper review. Questions without a confirmed key are **ungraded**, not incorrect. Grading-time snapshots preserve typed answers, confirmed keys, rules and decisions; existing legacy non-single keys are not automatically confirmed or regraded.

A printed question may list statements A–E but ask for **one** of four numbered combinations. New extractions flag these for review and can classify them as single-correct; explicit multi-select wording and recognized multiple-correct or numerical section instructions take precedence. Recognition is heuristic and can miss or misread instructions, so check the paper. Existing papers and confirmed answers are not automatically rewritten. For an existing misclassified question, open **Edit**, compare the PDF, choose **Single-correct MCQ** only if exactly one printed option is intended, and save. Re-read the image key afterward, compare the printed order (`1=A`, `2=B`, `3=C`, `4=D` when it matches the displayed order), and explicitly confirm the checked answer. A changed type requires the key to be checked again.

Question and option images may be cropped from the source or uploaded. For choices visible only in an original crop, explicitly check the labels that are actually printed instead of retyping them; the app does not invent choices. The review and bank pages flag known missing/incomplete or potentially answer-bearing crops and link to repair tools, but those checks are not exhaustive. Always compare text, formulas, options, keys and figures against the original. The local parser can cautiously recover some wrapped choices, tables and figures, but can also miss or misread them.

Each private test stores its chosen duration. On start, the server stores one absolute expiry; reloads, resuming and reconnecting do not reset it. The database prevents two simultaneous active private attempts for the same student and test; after submission or expiry the same test cannot be retaken. A newly created test is a different identity. Selecting an answer is autosaved, and manual submission also saves the last visible answer atomically. Private tests do not consume the free trial, do not enter public catalog/leaderboards or institutional results, and are excluded from teacher/admin management views. Existing institutional exam duration settings remain unchanged.

### Optional free local AI comparison after submission

A true correct count requires a trusted answer key. Optionally, after submitting a private test, a student can request an **unverified local AI comparison** of answered questions that have no confirmed key. The UI reports how many guesses match or differ from the student's selections, plus skipped/uncertain questions; this is **not an official or verified score**. The model sees the question and options, not the student's selection. By default, image-dependent and complex merged/empty-table questions are skipped; an optional operator-configured local vision model can receive the original question crop on the same trusted server. Bad extraction or an incorrect model answer can invalidate a comparison. Suggestions never change stored keys, verified results, marks, rankings, or past attempts. They are visible only to the owner, are blocked during active private tests, and can be cleared.

This feature is **off by default**; neither a model nor Ollama is bundled. To enable it, an operator must:

1. Install a current [Ollama](https://ollama.com/download) runtime on the **same server** as Django. Select and download a suitable local model separately, after checking its licence, memory requirements and quality. No model is downloaded by this application, and no model ID is preset.
2. Set `OLLAMA_NO_CLOUD=1` on the *Ollama service itself*, restart it, and verify that `http://127.0.0.1:11434/api/status` returns `{"cloud":{"disabled":true,...}}`. Older versions without this status endpoint are rejected. Restrict Ollama's network egress for stronger guarantees and keep its loopback listener and host under administrator control. A local Ollama endpoint alone does **not** guarantee local inference; cloud models and web search must be disabled. The PDF is not passed to the model; if a separate local vision model is explicitly configured, original question images may be sent to that local runtime.
3. In the private Django environment, set `LOCAL_PRACTICE_AI_ENABLED=True` and `LOCAL_PRACTICE_AI_MODEL` to the exact local name from `ollama list`. `LOCAL_PRACTICE_AI_PORT` defaults to 11434; `LOCAL_PRACTICE_AI_TIMEOUT` defaults to 120 seconds (allowed 5–300); `LOCAL_PRACTICE_AI_MAX_QUESTIONS` defaults to 300 (allowed 1–500). Apply migrations and restart the app. Cloud/URL model names are rejected, and the backend sends text only to `127.0.0.1`, with proxies and redirects disabled, after checking both cloud-disabled status and the installed model list. The server runtime itself must be trusted; use firewalling where strict offline operation is required.
4. On the results page acknowledge the warning, then start the comparison. It processes one question per request; keep the page open, or pause and resume later. A new comparison consumes **one** AI suggestion allowance from the practice plan, not one per question; resuming does not consume another. Clearing and restarting consumes another allowance, and usage is not refunded. No paid API or key is required for local inference, though hardware, electricity and the separately downloaded model have costs.

The integration has been exercised using a synthetic local HTTP stub, **not a real Ollama model**. Runtime/model compatibility, speed, and answer accuracy must be checked on the deployment before relying on it. An AI guess should never be treated as a substitute for an independently checked key.

### Auto-submit operations

The CBT server refuses answer changes after the stored expiry and finalizes overdue attempts on session, heartbeat, result, start, or practice-list requests. **For offline auto-submission when the student has closed their browser, run a worker and Celery beat**, with a shared database, private storage and Redis broker, for example:

```sh
# Set CELERY_ALWAYS_EAGER=False and CELERY_BROKER_URL appropriately first.
celery -A config worker -l INFO
# Run as a separate supervised service:
celery -A config beat -l INFO
```

Beat schedules `expire_private_practice_attempts`, `expire_classroom_attempts`, and `expire_series_attempts` every 30 seconds; finalization may be delayed by scheduling or broker/worker availability, but expired answers remain locked. Monitor both services. As an alternative or recovery mechanism, run `python manage.py expire_practice_attempts`, `python manage.py expire_classroom_attempts`, and `python manage.py expire_series_attempts` from cron. With development `CELERY_ALWAYS_EAGER=True` and no worker/beat or cron, offline attempts finalize on a later relevant request, including a classroom monitor/export, rather than in the background. These sweeps do not change ordinary institutional exam behavior.

### Private files and optional providers

Private practice, classroom and series PDFs and figures are stored under `PRIVATE_PRACTICE_ROOT` (default `private_practice/`), separate from `MEDIA_ROOT` and `STATIC_ROOT`, and served only through authorized owner or attempt-scoped views. New files use restrictive filesystem permissions; configure a protected persistent volume, access-controlled backups, and a web server that does **not** serve or symlink that directory. This is application-level isolation, not encryption against administrators with direct database/filesystem access. Deployments must also address authentication, HTTPS, quotas, abuse controls and user-data retention.

New teacher and student uploads accept only the local extraction method; modified requests for a cloud or other disabled method are rejected. Historical provider code and records remain in the project, but these upload workflows do not send PDFs to external services. No real provider calls are made by the test suite.

### Results, bank and data protection

- **Practice history** lists the student's attempts. A finished attempt has a scorecard, question-wise selections, the confirmed key and explanation where available, time, subject/topic/difficulty breakdowns, and comparison with other attempts. Scores and percentage use *confirmed-key marks* as the denominator. Accuracy uses only answered questions with confirmed keys. Questions without a key remain ungraded; they are not incorrect. A practice pass/fail outcome is shown only when a target is configured and every question has a confirmed key. The key is *student-confirmed*, not independently certified by the platform.
- Question time is an estimate based on browser save/heartbeat events; disconnects, a closed browser or time after the last save may not be recorded. Total time is measured separately from the server start and submit/expiry timestamps. Institutional examinations retain their existing behavior.
- **My Question Bank** is private to the owner. Included, approved PDF items are added when a private test is created; students can search/filter by subject, topic, chapter and difficulty, edit or remove an item and create a new test from selected items. Each bank edit uses copy-on-write if an existing test refers to the item, so existing question text, options, images, marks and attempt results remain unchanged. Removing an item from the bank retains its historical version where a test needs it. Deleting the original paper/test is explicitly destructive for its own attempts, but independent bank tests retain their referenced questions and images. Operators must account for these retained versions in their retention and erasure procedures.
- All result, report, source-paper and bank views require the owner. To avoid disclosing keys during a different private test, these answer-bearing areas are temporarily blocked while **any** private test for that student is active. Other students, teachers and application-admin views cannot access private material. These controls do not protect against a server/database operator with direct access, or the student's own copy of the PDF or previously downloaded report.
- Downloadable reports include the scorecard, breakdowns, question-wise text, selected/confirmed answers, explanations, images and recorded times. PyMuPDF shapes English/Hindi using the embedded Noto Sans Devanagari font (SIL Open Font License, included in `static/fonts/`). Some complex Devanagari ligatures can render correctly while copy/search text extraction is imperfect; verify accessibility requirements for your use case. LaTeX is included as text in the report rather than typeset math, and missing source images are noted. Browser assets and their licences are in `static/vendor/`.

### Packs, verification and Razorpay test mode

The lifetime packs and one-time free allowances above replace the old monthly PDF, classroom and attempt pricing. Retained legacy practice-plan feature switches and AI quotas are separate; see [operator instructions](docs/commercial-operations.md). New legacy subscriptions are disabled and old test entitlements do not become real paid access. Signed email verification is required for new student and teacher registrations; existing accounts are grandfathered. Console mail is development-only and does not deliver email.

Razorpay live keys are rejected. `ALLOW_TEST_PAYMENT_CREDITS` is **false by default**, and must be enabled only on a disposable isolated sandbox database, never production data. Test signatures, captures, orders, amounts and currencies are checked server-side; replay is idempotent, refunds suspend the affected unused access, and records are retained. A provider timeout can need manual reconciliation. Actual Gmail delivery, Razorpay checkout/webhooks, production concurrency and deployment readiness are not verified. See the [commercial operations guide](docs/commercial-operations.md) for setup and recovery; do not activate real payments without further implementation and approval.

After backing up an existing database and files, apply pending migrations with `python manage.py migrate` before deploying this revision. Migration `questions/0016` flags existing public PDF-derived questions without rewriting saved keys or scores. `questions/0017` and `exams/0013` add typed keys, responses, rules and snapshots; legacy non-single values default to unconfirmed and existing scores are not recalculated. `accounts/0003`, `exams/0014` and `questions/0018` add exam tracks/targets and classify only unambiguous legacy labels and explicit source relationships. Legacy attempts without grading snapshots retain their recorded results and disclose that original key details are unavailable.

Run the suite with `python manage.py test --settings=config.browser_test_settings`; its disposable file-backed SQLite database is important for concurrency tests. No real payment or external OCR-provider calls are made by the tests. The real-browser smoke test is optional because it requires Node 20+ and a compatible Chromium binary:

```sh
(cd tests/browser && npm ci)
DJANGO_SETTINGS_MODULE=config.browser_test_settings RUN_BROWSER_E2E=1 CHROMIUM_PATH=/path/to/chromium TESSDATA_PREFIX=/path/to/tessdata python manage.py test apps.exams.test_classroom_browser apps.exams.test_exam_target_browser apps.questions.test_browser_practice apps.core.test_commercial_browser apps.core.test_static_browser apps.core.test_redesign_browser
```

The classroom browser test uses a synthetic PDF, teacher and student accounts, including a mobile viewport, to check review, code joining, answer retry/resume, scoring and ranked CSV. The practice browser test starts an isolated test server/database, generates a synthetic PDF with images, and uses temporary private storage. It checks registration, upload/review, autosave/refresh, CBT, results, report, no-retake/privacy, history, bank, pack UI and the unconfigured payment state without contacting providers. The static/UI browser test collects production assets into a temporary directory and uses WhiteNoise (not Django's development static handler) to check public pages, all four role dashboards, mobile menus, exam actions, KaTeX, Chart.js and the browser console at desktop and mobile widths. These tests delete temporary test data afterward. `config.browser_test_settings` uses a disposable, uniquely named file-backed SQLite test database so overlapping browser requests do not share Django's in-memory connection; it does not change the application database. The same settings can be used for the complete suite. Some container-only Chromium distributions also require system libraries, `LD_LIBRARY_PATH`, and `CHROMIUM_SINGLE_PROCESS=1`.

On PowerShell, set the environment variables before the same `python manage.py test ...` command: `$env:DJANGO_SETTINGS_MODULE="config.browser_test_settings"`, `$env:RUN_BROWSER_E2E="1"` and `$env:CHROMIUM_PATH="C:\path\to\chrome.exe"`.

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

Migration `exams/0012` initializes the seven standard categories **only if the category table is completely empty**. If any categories already exist, including inactive or private system categories, it leaves the entire catalog unchanged. It never changes teacher assignments, existing exams or scores. Back up an existing database before migrating; the migration's reverse intentionally does not delete categories that may have acquired content.

If **Create Examination** has no category choices, an administrator can open **Examination Categories**, create an active category or explicitly activate an appropriate existing one, then return and use **Recheck categories** without losing the exam form. Teachers must ask an administrator; only their currently allowed active categories are offered (with the existing fallback to all active categories when they have no active assignments). Existing exams can retain their current inactive or no-longer-assigned category during an authorized edit, but another unavailable category cannot be selected. When JavaScript is unavailable, copy the form text before reloading.

`python manage.py seed_categories` is an optional command to add missing standard categories; it skips name/slug collisions and never changes or reactivates existing categories. `python manage.py seed_admin` creates demo accounts; avoid demo credentials outside an isolated local database. Run `python manage.py test` for the automated suite.

### Static files and troubleshooting

Bootstrap, Bootstrap Icons, KaTeX and Chart.js are bundled under `static/vendor/` along with their referenced fonts and source maps. Development uses Django's `staticfiles` finders; it does not need a CDN or `collectstatic`. `STATIC_URL` is `/static/`, and `STATICFILES_DIRS` includes the project `static/` directory. Check, for example:

```sh
python manage.py findstatic vendor/bootstrap/bootstrap.min.css --verbosity 2
```

In a production environment configured with `DEBUG=False`, run **`python manage.py collectstatic --noinput` after every asset update**, then deploy/restart the application with the resulting `STATIC_ROOT` available to WhiteNoise. Production uses compressed manifest storage, which fingerprints filenames and rewrites references to fonts and source maps. Do not add an upstream pre-collection `integrity` hash to these same-origin bundled files: it does not match the rewritten bytes and the browser will block the asset. If assets are served by a separate CDN or proxy, configure it to serve the collected manifest and files consistently. Production HTTPS, secrets, storage and proxy settings still require proper deployment configuration; `runserver --insecure` is not a production static server.

If a local page still looks unstyled, confirm the checkout/branch and `DEBUG` value, restart `runserver`, and hard-refresh the browser. In the browser's Network and Console tabs, check that `/static/vendor/bootstrap/bootstrap.min.css`, the Bootstrap JS bundle, icon font and `css/style.css` load successfully; with `DEBUG=False` their URLs contain fingerprinted names. A 404 usually means the files or collected output are missing or the server is serving the wrong directory. A browser integrity error indicates stale/mismatched markup or cached assets; redeploy the matching templates and collected assets, restart the server, and clear relevant browser/proxy caches. Do not delete user databases, uploads or private files as a troubleshooting step.
---

## PDF extraction: setup, privacy and review

**The default is local extraction.** It does not send the PDF to Mathpix, Google, or another provider and requires no paid API key. The parser uses PDF geometry for plain numbered and prefixed questions, additional option-label styles, some ruled tables, embedded images and candidate vector drawings. On mixed PDFs, readable digital pages keep their layout while scanned pages are OCRed locally. Some scanned-page ink clusters, including those on full-page scans with an existing text layer, are saved as flagged diagram candidates. Source crops and uncertain assignments need human review; this is not a guarantee of correct extraction.

The local layout pass can infer one to three equal-width columns per page and recognize some paired, repeated-number language columns. For a detected parallel layout it extracts the more readable text layer and, when boundaries are reliable, keeps the other original-language region as a labelled image; it does **not** translate or guarantee that choices correspond. It also preserves some uncertain equations, answer groups and tables as original-PDF image crops. When choices cannot be parsed but reliable boundaries are found, a bounded original question image may be saved for manual repair; it does not create or infer answer options. Open and compare every crop, including choice order, before creating a test. Crops may contain printed answers, solutions or unrelated material even when automatic checks do not detect them; remove or recrop unsafe images. Source images can be copied into the bank and shown in the CBT.

Explicit paper-wide question totals are compared with detected counts when recognizable, and gaps, ambiguous numbering and some unsupported formats are flagged. These checks can miss problems. The CBT supports reviewed single-correct, multiple-correct and numerical questions with the scoring rules described above. Edit or exclude subjective, incomplete and image-only items that cannot be represented correctly. No missing questions, option text or answer keys are invented. See `docs/upgrade-validation.md` for local benchmarks and limitations.

For scans, install Tesseract with **English (`eng`) and Hindi (`hin`) language data** on the machine running the app/worker. The primary OCR path uses PyMuPDF's local Tesseract integration and needs access to those `.traineddata` files; its basic fallback additionally uses the Tesseract executable. Most installations are detected automatically. If not, set these in the server's private `.env` (example paths for Windows; use your own install path):

```dotenv
TESSERACT_CMD=C:/Program Files/Tesseract-OCR/tesseract.exe
TESSDATA_PREFIX=C:/Program Files/Tesseract-OCR/tessdata
```

The `tessdata` directory should contain `eng.traineddata` and `hin.traineddata`; restart the app and any worker after changing settings. For **answer-key photos**, `pip install -r requirements.txt` also installs offline English data into the active Python environment. The PyMuPDF image reader can use that data without a separate Tesseract executable; an explicitly configured local data directory takes precedence. This improvement was tested on Linux, not the user's Windows machine. Scanned **PDF extraction** has separate OCR paths and can still need a working Tesseract installation and Hindi data. Key-photo reading operates in memory and never downloads data or sends photos to a provider. If Hindi data cannot be used, the log warns that English-only OCR was attempted. If OCR is unavailable or a scanned page fails, the review screen warns that questions may be missing; readable digital pages are still processed. OCR and region detection can miss, merge or misread text and images, especially on noisy, rotated or low-resolution scans. Review every page, formula, table and diagram; use the original page/crop and unassigned-image pool to correct omissions. The parser never confirms an answer key automatically.

**Existing extracted questions are not replaced.** To apply an updated parser to an old paper, upload a fresh copy and review the new results; existing staging work, tests and bank records remain intact.

### Historical Gemini-only extraction code (not offered for new uploads)

**Current uploads are local-only.** The following notes document preserved provider code and historical configuration, not an available upload choice. Setting these variables does not enable cloud extraction in the current student or teacher upload forms.

This separately disabled option avoids Mathpix charges. PyMuPDF reads available PDF text and embedded image positions locally; Gemini receives that text and rendered page images and transcribes questions. The source PDF is **not** uploaded to Mathpix. Scans can have no usable local text, and scanned or vector diagrams may not appear among image candidates; inspect and crop missing figures manually. It is not a guarantee of lower cost when the operator selects an expensive model, or of equal accuracy to Mathpix. Every AI question requires review and has no automatically confirmed answer key.

Historical configuration was kept for compatibility with existing records; do not put any credentials in Git or chat:

```dotenv
PDF_GEMINI_ENABLED=True
GEMINI_API_KEY=<your-server-side-api-key>
PDF_GEMINI_MODEL=<a-supported-vision-model-id-available-in-your-account>
# Optional seconds between requests within one job; 0 means no delay.
PDF_GEMINI_MIN_INTERVAL_SECONDS=0
PDF_CLOUD_MAX_PAGES=50
PDF_GEMINI_THINKING_LEVEL=
PDF_GEMINI_MAX_OUTPUT_TOKENS=32768
```

The model and generation settings are independent of Mathpix + Gemini, so configuring a cheaper model does not change the existing pipeline. The model is selected by the operator, not hardcoded. As an example, Google listed `gemini-3.5-flash-lite` with image input and structured output on 2026-11-03, but this integration has **not** been tested against the live model or provider. Verify account access, compatibility, terms and pricing first. With that model's then-published standard paid rates of $0.30/million input and $2.50/million output/thinking tokens, an **illustrative, unmeasured** 200,000 input plus 100,000 output/thinking tokens would cost $0.31 (about ₹29.45 at ₹95/USD) for Gemini; actual usage may be higher or lower and excludes taxes, other services and retries. The app sends two primary pages per request with neighboring context, so a 30-page PDF sends 58 page previews over 15 requests. It does not use Google's discounted Batch API. Check [Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing) and the provider billing dashboard for actual charges.

An eligible Google unpaid tier may have no token charge within its quota, but quotas and availability vary and it is not a guaranteed production capacity. Google states that unpaid service content and responses may be used to improve products and may be reviewed by people; do not send sensitive, confidential or personal material to that tier. Review the current [Gemini terms](https://ai.google.dev/gemini-api/terms) for the selected account and region. The current upload forms cannot select this mode; historical use required explicit per-upload consent. A timeout or failed request may still be billable; the app does not automatically retry. `PDF_GEMINI_MIN_INTERVAL_SECONDS` can pace one job for rate-limited accounts, but does not enforce a project-wide limit across simultaneous jobs. Configure worker concurrency and provider-side budgets separately. In development eager mode the upload request blocks while waiting; use a worker in production.

Where Gemini returns usage metadata, the review screen/log shows provider-reported token counts, including thinking counts where supplied. These counts may be incomplete after failure and are **not** a bill, a price cap, or the amount a student owes. There is no per-paper spending limit in the app; provider billing is authoritative. Student cloud-feature permissions and PDF quotas apply to both cloud modes.

### Historical Mathpix + Gemini extraction code (not offered for new uploads)

The preserved **Mathpix + Gemini** pipeline is disabled for new uploads. The variables below describe historical configurations only; setting them does not add an upload option. Never put real credentials in Git or send them in chat:

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
4. New upload forms no longer expose Mathpix or Gemini extraction and reject forged non-local methods. Existing historical records and provider code remain; review any previously queued jobs and their original consent before operating an older deployment.

Mathpix + Gemini processing uploads the full PDF to Mathpix. It sends the OCR text, normalized layout regions and rendered page images to Google's Gemini API, in two-primary-page batches with neighboring-page context. Images used in Gemini calls are compressed previews; figures saved in the app are cropped from the original PDF with PyMuPDF, not generated by AI. An uploaded paper is never sent without the cloud option and explicit per-upload consent. The app requests deletion of the Mathpix document after OCR, including on processing errors when the provider ID is known. Deletion can fail, a timed-out submission can have an unknown ID, or a killed worker may not run cleanup. A known Mathpix document reference is recorded on the paper as soon as the provider returns it; check the paper log/reference and contact Mathpix if needed. CDN caches and minimal billing/audit metadata may remain. Review [Mathpix retention and deletion](https://docs.mathpix.com/concepts/data-retention) and [Google's Gemini API terms and privacy](https://ai.google.dev/gemini-api/terms) for your account and region before uploading sensitive material. The PDF, review data and crops are also stored in the app's media/database until managed by the operator; this feature does not implement a complete retention policy.

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

Extraction is not a general math/layout OCR system. Some ruled fractions and short radicals can be reconstructed, but complex equations, Type3 glyphs, borderless tables, vector drawings, scans, Hindi OCR and page/column continuation can still be wrong or omitted. Even unflagged items need checking against the original. Cloud-returned source coordinates are model suggestions, not independently verified; missing coordinates can be reviewed on the original page. Deleting the original paper removes its source PDF, even if separately retained bank snapshots survive. PDF reports render tables but currently include LaTeX as text rather than typeset formulas; Hindi copy/search and accessibility need further validation.

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
    - Local extraction is heuristic; mathematical notation, tables and image associations may need manual correction. Optional consent-gated Gemini-only and Mathpix + Gemini pipelines and the manual image editor support multiple question and option images; scanned/vector figures may require manual crops.
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
    - Server-side deadline enforcement: overdue answers are rejected and attempts are finalized on a subsequent request. Private-practice, classroom and series offline finalization additionally require the worker/beat or cron setup described above.
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
