# Commercial workflow: operator setup and safeguards

**Status:** implementation and automated tests use synthetic documents, mock Razorpay responses and test-only mail. No real SMTP delivery or Razorpay sandbox/live transaction has been verified. Live Razorpay keys are deliberately rejected. Do not offer real paid access or enable sandbox credits on a database containing production data. Complete provider, legal, tax, refund, security, capacity and deployment reviews before any launch.

## Products and attempt rules

| Customer | One-time free use | Proposed one-time pack |
| --- | --- | --- |
| Student PDF practice | First local PDF extraction | ₹299 for 20 additional PDF extractions |
| Student classroom | First classroom test start | ₹99 for 20 additional classroom test starts |
| Teacher classroom | First test activation | ₹299 for 20 additional activations |
| Student JEE series | Test 1 | ₹299 unlocks tests 2–16 in the JEE series |
| Student NEET series | Test 1 | ₹299 unlocks tests 2–16 in the NEET series |

Free allowances are separate, once per account, and do not reset monthly. Unused paid pack credits currently have no expiry. A classroom activation makes **one test** available for 24 hours and can be reused for multiple class codes; each new student start consumes that student's classroom allowance/credit. There is no application-imposed 40-student cap, although real hosting capacity must be tested. A series attempt does not consume classroom or PDF credits. Changing a student's track does not transfer the JEE series purchase to NEET or vice versa.

For classroom, series and private PDF/bank tests, a student gets **one attempt per test identity**. Codes for the same classroom exam share this restriction. A disconnect or refresh resumes the same running attempt with its original server expiry and no additional charge. Submitted, expired or security-ended attempts cannot be restarted; the student can open the available result and review. An independently created snapshot (including a new PDF review submission or new bank test) is a different test identity; the application does not deduplicate equivalent PDFs or questions. Existing institutional exam attempt settings remain separate. Historical attempts are not deleted or regraded; pre-existing historical repeats may still exist.

New classroom sessions and series show the student's score and their own answers immediately after submission, with partial credit and unanswered/ungraded items distinguished. Only human-confirmed, valid keys are scored. For new classroom sessions the teacher may release correct keys/explanations immediately when creating a PDF test, or release them per session later; a series administrator controls the equivalent series setting. This is a configurable safeguard, not a guarantee of secrecy: a score or correctness indicator can reveal whether a choice was right, and the original text or images can themselves contain printed answers. Check and repair crops before publication. Migrated classroom sessions retain their previous results-release policy. No new answers, explanations or content are fabricated.

## Upgrade and historical data

1. Back up the database **and** `MEDIA_ROOT` and `PRIVATE_PRACTICE_ROOT`; verify a restore separately. Do not overwrite an existing `.env`, database or files. Stage and rehearse the migration on a copy first.
2. Run `python manage.py migrate`, then restart web, workers and beat. Collect static files for the deployment. Migration `exams/0016` creates empty JEE/NEET series slots, marks free allowances used when historical PDF/classroom evidence exists, and grants existing classroom sessions a 24-hour transition activation without retroactive charges. It does not infer the contents of missing papers. Existing student/teacher accounts are grandfathered rather than forced through a new verification gate. New public registrations require email verification. Classroom no-retake rules apply to new starts, including old session codes; past results and in-progress timers remain intact.
3. Historical practice plans, payments and subscriptions are retained. New legacy subscriptions are disabled. Because the legacy gateway was also test-only, legacy paid entitlements are not applied unless the isolated sandbox flag below is enabled; assess any real-world obligations separately before deployment. PDF credit fallback can honor an unexpired legacy entitlement **only in that sandbox**. Other legacy feature/AI quotas remain separate; they do not replace the lifetime PDF/classroom packs.
4. Protect backups, operational access and private files. Application and Django-admin account deletion is guarded when an account owns papers, tests or authored questions, has attempts or classroom participation, or has usage/billing records. Deactivate an account instead of deleting its history. Direct database/filesystem operations bypass application safeguards; they are not a substitute for verified backups. Do not remove records to create extra trials.

## Verification email (console locally, Gmail to explore)

`PUBLIC_BASE_URL` must be the site's exact public origin, without a path. A deployed site with `DEBUG=False` requires HTTPS. It is explicitly configured, never derived from the incoming Host header. `ALLOWED_HOSTS` and exact `CSRF_TRUSTED_ORIGINS` must match the deployment. Set `TRUST_PROXY_HTTPS=True` only when a trusted reverse proxy strips untrusted forwarding headers and supplies the correct HTTPS scheme.

For local development, `.env.example` uses the console mail backend and `PUBLIC_BASE_URL=http://127.0.0.1:8000`. Register and open the signed link printed on the server console. **This does not send email.** Links expire after 24 hours, require an explicit CSRF-protected confirmation POST, and become invalid after use, resend or address change. A resend is throttled per account. Existing active attempts and their expiry remain usable after an address changes while the new address is unverified. New starts, PDF uploads, classroom activations, pack checkouts, and public test/question creation and publication recheck the locked account's verification status. Admin account edits and verification resends use the same lock, so stale form state does not restore an old verification token. These are application safeguards, not a substitute for a security review.

To explore a dedicated Gmail sender, first create a separate account and enable two-step verification. If the account is eligible, create a Google app password and supply these values **only in the private server environment or secret manager**, not in Git, screenshots or chat:

```text
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_USE_SSL=False
EMAIL_HOST_USER=<dedicated Gmail address>
EMAIL_HOST_PASSWORD=<Google app password, kept secret>
DEFAULT_FROM_EMAIL=<same dedicated Gmail address>
PUBLIC_BASE_URL=https://<your-verified-site-origin>
```

Restart the web service after changing its environment. Confirm that the host allows outbound SMTP on port 587, the account permits app passwords, and a real message reaches an independently controlled inbox; test spam placement, confirmation, resend, expiry, changed-address invalidation and password reset. Google's consumer sending limits and abuse restrictions can change, and a password change can revoke app passwords. Gmail is not guaranteed to provide deliverability, throughput or a free production service. If it is insufficient, assess a transactional provider, verified domain, SPF/DKIM/DMARC, current pricing and approval separately. Never claim a successful send simply because a backend accepted it.

Verification reduces, but does not eliminate, duplicate-account abuse. Disposable inboxes, aliases and multiple real addresses remain possible. Add appropriate registration/login/resend rate limits, monitoring, abuse response, retention and privacy policies before public launch. Administrators can provision or explicitly verify accounts and must be trusted; public self-registration does not automatically verify them. When a student or teacher email is changed through the profile or application management page, the new address must be verified again.

## Razorpay: isolated test mode only

1. Use a **separate disposable sandbox database and private-file directory**, not a copy that will later become production. Keep `ALLOW_TEST_PAYMENT_CREDITS=False` everywhere else. On the isolated sandbox only, set it to `True`, configure `RAZORPAY_KEY_ID` beginning `rzp_test_`, `RAZORPAY_KEY_SECRET`, and a separate `RAZORPAY_WEBHOOK_SECRET` in a secret manager or private environment. Restart the app. Do not share credentials in chat or commit `.env`.
2. Set an externally reachable HTTPS **test-mode** webhook to `/payments/razorpay/webhook/`. Configure `payment.captured`, `order.paid`, `payment.failed` and `refund.processed`; the legacy subscription event handlers are retained for historical test records. Verify the actual provider's current documentation and event payloads. The public test key ID is exposed to the checkout browser; secrets stay server-side.
3. Use Razorpay's test payment methods only. Verify successful capture, failed/cancelled payments, browser interruption, duplicated/delayed webhook events, refund processing and corresponding balance changes. A browser success callback alone does not grant credits: the server verifies the signature, fetches the captured payment, checks the order, amount and currency, and records an idempotent grant. Signed webhooks are also validated. Any recorded partial or full refund suspends that grant's unused access; already-started attempts and history are retained. Reconcile the actual merchant terms and partial-refund policy before live use.
4. If an order request times out, its local status may remain `initiating`. Investigate the **test** dashboard using the local checkout UUID/receipt. Do not create another order blindly. To link a matching test order without granting credits:

   ```sh
   python manage.py reconcile_pack_checkout <checkout-uuid> <order-id>
   ```

   The command verifies the provider order and frozen terms, but does not activate a grant. Complete the usual server-verified checkout or replay the original signed capture webhook. Legacy checkout reconciliation uses `reconcile_practice_checkout` instead. Monitor unresolved orders and failed webhooks; never assume a network timeout means no provider action occurred.

All currently created grants are marked test-only and are suppressed when the sandbox flag is off. Disabling the flag does not erase records and is not a substitute for isolating databases. **Live activation is not implemented or approved:** live keys are rejected, and a real provider integration, KYC, pricing, taxes, refund terms and security/load review remain outstanding. Never treat a mock result or test credit as real money.

## Supplying and publishing the series

The migration creates 16 empty slots for each track, **not 32 real tests**. An application administrator uploads their own PDF through the private classroom-paper workspace. Extraction is local. Review every question, original image, options, question type, marks and keys; repair missing or answer-bearing images and enter/confirm all keys. From that reviewed paper choose **Publish to series**, select an empty slot for the matching JEE/NEET target and acknowledge the review. Publication copies an exclusive private snapshot, does not spend a teacher classroom credit and leaves the original paper intact. Assigned slots cannot be replaced through normal management. The first test can be used free when it is ready; the paid pack is offered only when **all 16** slots are published, correctly targeted, have confirmed keys and their private assets are available.

The operator page shows readiness and allows one-way key release. Normal public question-bank, exam-edit, export and teacher views cannot be used to expose or alter a series snapshot. Students see only their own track and attempt-scoped assets; administrators and infrastructure operators remain trusted. Regularly back up private storage and check files, because losing a crop can make a slot unavailable. No automated check can guarantee a crop is free of printed answers or extraction mistakes.

## Expiry and failed extraction recovery

For unattended finalization, run a supervised Celery worker **and** beat against shared persistent resources (`CELERY_ALWAYS_EAGER=False`); schedules cover private practice, classroom and series attempts every 30 seconds. Alternatively run `python manage.py expire_practice_attempts`, `python manage.py expire_classroom_attempts`, and `python manage.py expire_series_attempts` from cron. Requests also enforce the stored expiry; worker/broker failures can delay background scoring but do not extend the timer. Monitor lock contention and test PostgreSQL concurrency and hosting capacity before launch.

A confirmed, empty failed student PDF extraction may return its free/paid credit once; partial extracted or reviewed work is preserved and is **not** automatically refunded. A queue timeout can be ambiguous. Inspect a stuck job first:

```sh
python manage.py reconcile_private_extraction <paper-id>
```

After investigating the worker and making a backup, `--cancel-empty` fences an empty pending/processing job and attempts a guarded reversal; it keeps the source PDF. `--restore-review` makes preserved staged work available without returning a credit. Neither action deletes source data or guesses that an uncertain provider/worker call failed. Legacy monthly PDF debits require manual reconciliation. These paths have synthetic tests, not a production distributed-worker stress test.
