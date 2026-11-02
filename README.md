# TestForge CBT — Commercial-Grade Online Examination Platform

[![Django](https://img.shields.io/badge/Django-5.2%20LTS-092E20?logo=django)](https://www.djangoproject.com/)
[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python)](https://www.python.org/)
[![Bootstrap](https://img.shields.io/badge/Bootstrap-5.3-7952B3?logo=bootstrap)](https://getbootstrap.com/)
[![License](https://img.shields.io/badge/License-Proprietary-blue.svg)]()

A high-performance, commercial-grade Computer-Based Testing (CBT) and online examination platform designed for competitive examinations (UPSC, SSC, Banking), engineering and medical entrance tests (JEE, NEET, GATE), school/university term exams, coaching institutes, and technical/coding assessments.

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

## 2. Environment Diagnostics & Service Status

| Service / Dependency | Required For | System Detection Status | Configuration Strategy |
| :--- | :--- | :--- | :--- |
| **Python** | Runtime | **Installed: Python 3.12.10** | Active in `venv` |
| **Django** | Web Framework | **Installed: Django 5.2.18 LTS** | Pinned in `requirements.txt` |
| **PostgreSQL** | Primary Database | **Not Running on 5432** | Dual-mode: SQLite fallback for local dev; switches to PostgreSQL when `DB_ENGINE=postgres` is set. |
| **Tesseract OCR** | Scanned PDF Extraction | **Not Detected in PATH** | PyTesseract wrapper installed. Path configurable via `TESSERACT_CMD` in `.env`. |
| **Redis** | Celery Message Broker | **Not Running on 6379** | `CELERY_ALWAYS_EAGER=True` for synchronous background task execution during dev and testing. |
| **Razorpay** | Payment Gateway | **Client Library Installed** | Configured with test credentials in `.env`. |

---

## 3. User Roles & Authentication

The platform enforces strict role-based access control with four distinct roles:

1. **Super Admin**: Platform-wide governance, manage administrators, audit trails, category oversight.
2. **Administrator**: Student management, teacher onboarding, category creation, operational reports.
3. **Teacher / Content Manager**: Scoped strictly to permitted categories and self-authored tests.
4. **Student**: Candidate practice portal, performance summary, attempt history, free trial tracking.

### Pre-Seeded Local Accounts

Provisioned via `python manage.py seed_admin`:

| Role | Username | Password | Email |
| :--- | :--- | :--- | :--- |
| **Super Admin** | `admin` | `admin12345` | `admin@cbtplatform.local` |
| **Teacher** | `teacher` | `teacher12345` | `teacher@cbtplatform.local` |
| **Student** | `student` | `student12345` | `student@cbtplatform.local` |

---

## 4. Setup & Execution Instructions

```powershell
# 1. Activate Virtual Environment
.\venv\Scripts\Activate.ps1

# 2. Run Database Migrations
python manage.py migrate

# 3. Seed Default Categories & Accounts
python manage.py seed_categories
python manage.py seed_admin

# 4. Run Automated Tests (27 tests across all apps)
python manage.py test

# 5. Start Development Server
python manage.py runserver 127.0.0.1:8000
```
Visit `http://127.0.0.1:8000/` in your browser.

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
  - **Automated Extraction Pipeline** (`apps/questions/extractor.py`):
    - Multi-page extraction with question number pattern recognition (`Q.1`, `Question 1:`, `1.`, `प्रश्न 1:`, etc.).
    - MCQ options parsing across single/multiple lines and inline formatting (`(A)-(D)`, `1-4`, Hindi Devanagari `क-घ`).
    - Mathematical equation and scientific symbol preservation (with KaTeX preview in review workspace).
    - Diagram and equation image extraction from PDF streams to local storage.
    - Scanned PDF detection with Tesseract OCR fallback and language data handling.
  - **Interactive Review Workspace** (`/questions/papers/<pk>/review/`):
    - Staging review table with filter tabs (`all`, `pending`, `flagged`, `approved`).
    - Editable question editor (`/questions/extracted/<pk>/edit/`): modify text, options, marks, diagrams.
    - Automatic incomplete question flagging (e.g. `< 2` MCQ options or missing statements).
    - Single question approval and bulk approval into permanent Question Bank.
    - Accidental duplicate question prevention with automatic bank item linking.
    - Quick answer-key importer supporting standard formats (`1: A, 2: B` or newline delimited).
  - **Access Control & Protected Media**:
    - Scoped access: Admins see all papers; Teachers see authored/assigned category papers; Students blocked (403 Forbidden).
    - Protected PDF streaming endpoint (`/questions/papers/<pk>/pdf/`) preventing unauthorized file downloads.
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
    - Autonomous server-side timeout: Automatically submits exam and transitions status to `TIMED_OUT` when time expires, independent of client browser state.
  - **Reliable Answer Persistence & Concurrency Safety**:
    - Real-time AJAX response autosave (`/exams/session/<uuid>/save-answer/`) with database row locking (`select_for_update`).
    - Complete browser refresh and network disconnection recovery: 100% of candidate responses, review flags, and palette states restored.
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
- [ ] **Phase 7: Freemium Subscription & Razorpay Payment Integration**
- [ ] **Phase 8: Security Testing, Performance Optimization & Cloud Deployment**
