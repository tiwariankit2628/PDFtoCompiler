#!/usr/bin/env python3
"""Local, synthetic HTTP classroom benchmark; never point at real user data.

Requires a freshly migrated PostgreSQL database named loadtest_*, a local web
server using the SAME database, and an explicit safety acknowledgement. This
script creates records but NEVER deletes them or creates provider transactions.
See docs/capacity-testing.md for the full disposable setup and limitations.
"""
import argparse
import json
import math
import os
from pathlib import Path
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

import django  # noqa: E402
import requests  # noqa: E402

django.setup()
from django.conf import settings  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.middleware.csrf import _get_new_csrf_string  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import reverse  # noqa: E402

from apps.accounts.models import User  # noqa: E402
from apps.exams.models import (Category, ClassroomParticipant, ClassroomSession, Exam,  # noqa: E402
                               ExamAttempt, ExamQuestion, ExamResponse)
from apps.payments.models import CreditGrant, CreditUse, PackCheckout  # noqa: E402
from apps.questions.models import AnswerStatus, Question, QuestionOption, QuestionPaperUpload  # noqa: E402


def check_safety(args):
    url = urlsplit(args.base_url)
    if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost", "::1")
            or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment or not url.port):
        raise ValueError("base URL must be an explicit http://127.0.0.1:PORT or localhost:PORT origin")
    db = settings.DATABASES["default"]
    host = db.get("HOST") or ""
    if (not args.i_understand_this_creates_synthetic_records or not settings.DEBUG
            or connection.vendor != "postgresql" or not str(db["NAME"]).startswith("loadtest_")
            or not (host in ("127.0.0.1", "localhost", "::1") or host.startswith("/"))):
        raise ValueError("requires acknowledgement, DEBUG, a LOCAL PostgreSQL database named loadtest_*, and isolated configuration")
    if (settings.ALLOW_TEST_PAYMENT_CREDITS or settings.RAZORPAY_KEY_ID or settings.RAZORPAY_KEY_SECRET
            or settings.RAZORPAY_WEBHOOK_SECRET or settings.EMAIL_BACKEND not in (
                "django.core.mail.backends.console.EmailBackend", "django.core.mail.backends.locmem.EmailBackend")):
        raise ValueError("disable payment credentials/credits and use the console or locmem email backend")
    # Freshly migrated DBs contain seed categories/plans/series, but no users,
    # questions, tests, payments or attempts. Refuse to touch any existing work.
    for model in (User, Exam, Question, QuestionPaperUpload, ClassroomSession, ExamAttempt,
                  PackCheckout, CreditUse, CreditGrant):
        if model.objects.exists():
            raise ValueError(f"database is not empty of user data ({model.__name__}); create a fresh disposable database")
    if not (1 <= args.users <= 500 and 1 <= args.concurrency <= args.users
            and 1 <= args.questions <= 200 and 1 <= args.saves <= min(10, args.questions)):
        raise ValueError("limits: 1–500 users, concurrency <= users, 1–200 questions, 1–10 saves <= questions")


def seed(args):
    # An unpredictable run marker lets the HTTP preflight prove that the
    # server can read a freshly seeded session before it sends any test POSTs.
    marker = secrets.token_hex(12)
    with transaction.atomic():
        teacher = User.objects.create_user(f"load_teacher_{marker}", email="teacher@example.invalid", role="TEACHER")
        students = []
        for i in range(args.users):
            user = User(username=f"load_{marker}_{i:04d}", email=f"student{i:04d}@example.invalid",
                        role="STUDENT", exam_track="jee")
            user.set_unusable_password()
            students.append(user)
        User.objects.bulk_create(students)
        category = Category.objects.create(name="Load test category", slug="load-test-category")
        exam = Exam.objects.create(title="Synthetic load test", slug="synthetic-load-test", category=category,
            creator=teacher, is_classroom_only=True, status="published", target_type="jee_main",
            duration_minutes=120, total_marks=4 * args.questions, shuffle_questions=False, shuffle_options=False)
        questions = [Question(category=category, creator=teacher, subject="Math", target_type="jee_main",
                     is_classroom_snapshot=True, question_text=f"Synthetic question {i + 1}?",
                     answer_status=AnswerStatus.MANUALLY_VERIFIED) for i in range(args.questions)]
        Question.objects.bulk_create(questions)
        options = []
        for question in questions:
            options.extend(QuestionOption(question=question, option_key=key, option_text=f"Choice {key}",
                                          is_correct=(key == "A")) for key in "ABCD")
        QuestionOption.objects.bulk_create(options)
        ExamQuestion.objects.bulk_create([ExamQuestion(exam=exam, question=q, order=i + 1,
                                         positive_marks=4, negative_marks=1) for i, q in enumerate(questions)])
        session = ClassroomSession.create_for(exam, teacher, commercial_rules=True, label="Synthetic load test")
        ClassroomParticipant.objects.bulk_create([ClassroomParticipant(session=session, student=user) for user in students])
    clients = []
    for student in students:
        # Authenticate synthetic users without benchmarking password hashing or
        # exposing real credentials. HTTP requests still go through Django's
        # session and CSRF middleware and the real application endpoints.
        django_client = Client()
        django_client.force_login(student)
        session_cookie = django_client.cookies[settings.SESSION_COOKIE_NAME].value
        csrf = _get_new_csrf_string()
        http = requests.Session()
        http.trust_env = False
        http.cookies.set(settings.SESSION_COOKIE_NAME, session_cookie)
        http.cookies.set(settings.CSRF_COOKIE_NAME, csrf)
        http.headers.update({"X-CSRFToken": csrf, "Accept": "application/json"})
        clients.append({"student": student.pk, "username": student.username, "http": http})
    return exam, session, clients


def check_server_session(base, client):
    """Verify the local server sees this run's freshly created synthetic user."""
    try:
        response = client["http"].get(base + reverse("accounts:profile"), timeout=15, allow_redirects=False)
    except requests.RequestException as exc:
        raise ValueError(f"local server preflight failed ({type(exc).__name__})") from None
    if response.status_code != 200 or client["username"].encode() not in response.content:
        raise ValueError("local server did not recognize the fresh synthetic session; check its database and settings")


def percentile(values, p):
    return round(sorted(values)[max(0, math.ceil(len(values) * p / 100) - 1)], 1) if values else None


def phase(name, clients, concurrency, operation):
    def run(client):
        started = time.monotonic()
        try:
            ok, detail = operation(client)
        except Exception as exc:
            ok, detail = False, type(exc).__name__
        return (time.monotonic() - started) * 1000, ok, str(detail)[:160]
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        results = list(pool.map(run, clients))
    elapsed = time.monotonic() - start
    timings = [item[0] for item in results]
    errors = {}
    for _, ok, detail in results:
        if not ok:
            errors[detail] = errors.get(detail, 0) + 1
    report = {"phase": name, "requests": len(results), "elapsed_s": round(elapsed, 3),
              "requests_per_s": round(len(results) / elapsed, 2) if elapsed else None,
              "p50_ms": percentile(timings, 50), "p95_ms": percentile(timings, 95),
              "p99_ms": percentile(timings, 99), "max_ms": round(max(timings), 1) if timings else None,
              "errors": errors}
    print(json.dumps(report), flush=True)
    return not errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="LOCAL HTTP origin, e.g. http://127.0.0.1:8010")
    parser.add_argument("--users", type=int, default=40)
    parser.add_argument("--concurrency", type=int, default=40)
    parser.add_argument("--questions", type=int, default=30)
    parser.add_argument("--saves", type=int, default=3)
    parser.add_argument("--i-understand-this-creates-synthetic-records", action="store_true")
    args = parser.parse_args()
    try:
        check_safety(args)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    base = args.base_url.rstrip("/")
    exam, classroom, clients = seed(args)
    try:
        check_server_session(base, clients[0])
    except ValueError as exc:
        for client in clients:
            client["http"].close()
        print(f"No load requests sent: {exc}. Synthetic seed records remain in the disposable database.", file=sys.stderr)
        return 1
    print(json.dumps({"benchmark": "synthetic-local-classroom", "database": settings.DATABASES["default"]["NAME"],
                      "users": args.users, "concurrency": args.concurrency, "questions": args.questions,
                      "saves_per_user": args.saves, "note": "burst phases, local traffic, no PDF/OCR/images/providers"}), flush=True)

    def start(client):
        r = client["http"].post(base + reverse("exams:classroom_take", args=[classroom.pk]), timeout=120, allow_redirects=False)
        location = r.headers.get("Location", "")
        return (r.status_code == 302 and location.startswith("/exams/session/"), f"HTTP {r.status_code} redirect {location[:80]}")

    if not phase("start", clients, args.concurrency, start):
        print("Start failures: later phases skipped; inspect the disposable database and server logs.", file=sys.stderr)
        return 1
    attempts = {a.student_id: a for a in ExamAttempt.objects.filter(exam=exam)}
    if len(attempts) != args.users or any(c["student"] not in attempts for c in clients):
        print("Inconsistent attempt count after starts; later phases skipped.", file=sys.stderr)
        return 1
    response_rows = ExamResponse.objects.filter(attempt__exam=exam, order__lte=args.saves).values_list(
        "attempt__student_id", "order", "id", "exam_question__question_id")
    option_ids = dict(QuestionOption.objects.filter(question__exam_instances__exam=exam, option_key="A")
                      .values_list("question_id", "id"))
    for client in clients:
        client["attempt"] = attempts[client["student"]]
        client["answers"] = {}
    by_student = {c["student"]: c for c in clients}
    for student_id, order, response_id, question_id in response_rows:
        by_student[student_id]["answers"][order] = (response_id, option_ids[question_id])
    if any(len(c["answers"]) != args.saves for c in clients):
        print("Missing response rows after starts; later phases skipped.", file=sys.stderr)
        return 1

    def get_session(client):
        r = client["http"].get(base + reverse("exams:exam_session", args=[client["attempt"].attempt_uuid]), timeout=120, allow_redirects=False)
        return r.status_code == 200 and b"Synthetic load test" in r.content, f"HTTP {r.status_code}"
    ok = phase("session_page", clients, args.concurrency, get_session)

    def sync(client):
        r = client["http"].get(base + reverse("exams:exam_sync_status", args=[client["attempt"].attempt_uuid]), timeout=120, allow_redirects=False)
        return r.status_code == 200 and r.json().get("status") == "active", f"HTTP {r.status_code}"
    ok = phase("timer_sync", clients, args.concurrency, sync) and ok

    for i in range(1, args.saves + 1):
        def save(client):
            response_id, option_id = client["answers"][i]
            r = client["http"].post(base + reverse("exams:exam_save_answer", args=[client["attempt"].attempt_uuid]),
                json={"response_id": response_id, "selected_option_id": option_id, "action": "navigate", "time_spent_delta": 1},
                timeout=120, allow_redirects=False)
            return r.status_code == 200 and r.json().get("status") == "success", f"HTTP {r.status_code}"
        ok = phase(f"save_{i}", clients, args.concurrency, save) and ok

    def submit(client):
        response_id, option_id = client["answers"][args.saves]
        r = client["http"].post(base + reverse("exams:exam_submit", args=[client["attempt"].attempt_uuid]),
            json={"last_response": {"response_id": response_id, "selected_option_id": option_id}}, timeout=120, allow_redirects=False)
        return r.status_code == 200 and r.json().get("status") == "submitted", f"HTTP {r.status_code}"
    ok = phase("submit_and_grade", clients, args.concurrency, submit) and ok

    def completed(client):
        r = client["http"].get(base + reverse("exams:exam_completed", args=[client["attempt"].attempt_uuid]), timeout=120, allow_redirects=False)
        return r.status_code == 200, f"HTTP {r.status_code}"
    ok = phase("completed_page", clients, args.concurrency, completed) and ok

    attempts = ExamAttempt.objects.filter(exam=exam)
    violations = {
        "attempt_count": attempts.count() != args.users,
        "unfinished_or_wrong_score": attempts.exclude(status="submitted", score_obtained=4 * args.saves).count(),
        "wrong_expiry": sum(a.expires_at - a.started_at != timedelta(minutes=120) for a in attempts),
        "wrong_response_count": ExamResponse.objects.filter(attempt__exam=exam).count() != args.users * args.questions,
        "wrong_saved_answer_count": ExamResponse.objects.filter(attempt__exam=exam, order__lte=args.saves,
                                      selected_option__option_key="A", is_correct=True).count() != args.users * args.saves,
        "incorrect_free_debit_count": CreditUse.objects.filter(user_id__in=by_student, kind="classroom", is_free=True).count() != args.users,
        "unexpected_paid_grants": CreditGrant.objects.count(),
    }
    ok = not any(violations.values()) and ok
    print(json.dumps({"consistency_violations": violations, "success": ok}), flush=True)
    for client in clients:
        client["http"].close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
