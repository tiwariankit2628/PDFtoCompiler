# Classroom capacity and PostgreSQL testing

These are **synthetic local checks, not a hosting recommendation or a production-capacity guarantee**. The application does not impose a 40-student classroom cap, but an actual deployment must be measured on its own infrastructure and network before it is used for examinations.

## What was measured

An isolated PostgreSQL 16.14 database, Python 3.11, four synchronous Gunicorn workers and the load generator ran on the same shared two-CPU host, with `DEBUG=True` and local HTTP. Each run used a **fresh, empty disposable database**. Synthetic verified students started the same classroom test in a burst, loaded the session page, synchronized the timer, saved three answers, submitted, and loaded the completion page. The load generator did not render a browser or fetch question images/static assets. It bypassed password hashing by creating synthetic sessions. It did not run PDF extraction/OCR, Celery workers, Gmail, Razorpay, TLS or an external network.

Each row is a separate burst; concurrency equalled the number of synthetic users. Values are client-observed p95 request latency in seconds. They include local HTTP and queueing; `save` is the slowest p95 among the three save phases. The 40-user run had fewer questions and is **not directly comparable** to the 90-question runs.

| Users | Questions | Start p95 | Session page p95 | Timer sync p95 | Save p95 (max of 3) | Submit + grade p95 | Completion page p95 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 40 | 30 | 2.71 | 0.43 | 0.08 | 0.22 | 0.64 | 0.22 |
| 100 | 90 | 4.95 | 2.23 | 0.19 | 0.50 | 3.13 | 0.48 |
| 200 | 90 | 8.62 | 4.76 | 0.35 | 1.15 | 6.25 | 1.07 |

All three runs reported zero HTTP operation errors and zero violations in their checks of attempt and response counts, saved answers, score, expiry, free debits and unexpected paid grants. This does **not** mean 200 real students are supported: the transactions are synthetic, only three questions per student were answered, the whole workload shares a local machine, and neither internet latency nor concurrent image delivery or extraction was measured. The rising tail latencies show that queueing and database work deserve investigation. Runs may vary with machine and background load.

Separately, deterministic PostgreSQL tests exercise submission/timeout/security termination against classroom end and resume, as well as signed synthetic webhook races and refund/debit serialization. These tests use independent database connections; they do not reproduce all possible production failures. See [upgrade validation](upgrade-validation.md) for suite results.

## Reproduce safely on a local disposable PostgreSQL cluster

1. Install the project requirements in a venv. Install Gunicorn separately for this optional benchmark (`python -m pip install gunicorn`). Use a **new local PostgreSQL cluster or a newly created, empty database** and a test role. Do not use a production database or a copy containing real records. The script writes synthetic records and deliberately does not delete them.
2. In both the web-server shell and the generator shell, configure the **same** disposable database. Example (supply the database password privately if your local test role needs one):

   ```sh
   export DB_ENGINE=postgres DB_NAME=loadtest_classroom_40
   export DB_HOST=127.0.0.1 DB_PORT=5432 DB_USER=your_local_test_role
   export DEBUG=True ALLOWED_HOSTS=localhost,127.0.0.1
   export ALLOW_TEST_PAYMENT_CREDITS=False RAZORPAY_KEY_ID= RAZORPAY_KEY_SECRET= RAZORPAY_WEBHOOK_SECRET=
   export EMAIL_BACKEND=django.core.mail.backends.console.EmailBackend
   createdb -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" "$DB_NAME"
   python manage.py migrate --noinput
   ```

   Use a unique `loadtest_...` name for every run. Explicit empty provider variables prevent an existing `.env` from loading credentials into these processes. Keep private/media directories isolated too. Check the settings and database name yourself before continuing.
3. In the web-server shell, start the disposable server, bound to loopback for local testing:

   ```sh
   gunicorn config.wsgi:application --workers 4 --bind 127.0.0.1:8010 --timeout 120
   ```

4. With the **same environment** in the generator shell, run:

   ```sh
   python tools/loadtest_classroom.py --base-url http://127.0.0.1:8010 \
     --users 40 --concurrency 40 --questions 30 --saves 3 \
     --i-understand-this-creates-synthetic-records
   ```

   The tool only accepts a loopback HTTP origin, a local PostgreSQL database named `loadtest_*`, `DEBUG=True`, disabled test-payment credits, empty Razorpay credentials and console/locmem email. It rejects existing users, tests, questions and billing/attempt records, and requires the explicit acknowledgment. It creates an unpredictable synthetic username and verifies that the local server recognizes its session before issuing load POSTs; this is a useful guard, not a substitute for verifying both configurations. Limits are 1–500 users, concurrency no greater than users, 1–200 questions and 1–10 saves per user. A failed or successful run leaves its synthetic records behind. Stop the server after testing; remove the **disposable** database only after independently verifying its identity and contents.
5. The output is JSON Lines: one record per phase with p50/p95/p99/max latency, throughput and errors, followed by consistency checks. The process exits nonzero on an error or failed check. To repeat with 100 or 200 students, create and migrate a **different** empty database, restart the server on that database, and adjust users, concurrency and questions. Never point this generator at a deployed/remote site or real student accounts.

## Before choosing a host

Run an authorized staging test with production-like configuration, a representative 90–200-question test and realistic original images, full answer counts, concurrent classes, reconnects, different network speeds and regions, realistic login traffic, workers/beat, local PDF/OCR jobs and provider behavior. Check accessibility and mobile performance too. Agree on acceptable error rates and p95/p99 delays in advance; check data and timers after the burst and after failures/restarts. Profile database locks, slow queries, connection count, CPU, memory, storage I/O, bandwidth, job queues and worker saturation. Classroom start and grading include transactional database work; do not relax locks without correctness tests. Choose and re-test a deployment based on measured service levels, headroom and cost, not these local numbers. Back up and restore-test the database and private files; the synthetic generator is not a production stress tool.
