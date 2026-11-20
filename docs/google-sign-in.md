# Optional Google Sign-In: setup and safety

Google Sign-In is **off by default**. The message “Google sign-in is not configured” means the feature is disabled or its local settings are incomplete or invalid; it does not mean the password account was changed. Follow the steps below to create a Google Cloud OAuth **web application** client, privately configure it and run the new local check. This does not require a paid authentication intermediary. Creating the sign-in client does not itself require buying an authentication service; Google account eligibility, policies and quotas can change, and unrelated cloud services may have their own charges. Consult the [Google setup guide](https://developers.google.com/identity/oauth2/web/guides/get-google-api-clientid) and [OAuth audience rules](https://support.google.com/cloud/answer/15549945) for current requirements.

**This is sign-in, not email delivery.** It does not enable Gmail SMTP or the Gmail API and does not send verification or password-reset mail. Normal password registrations and some Google accounts still need a separately configured mail service; see [commercial operations](commercial-operations.md#verification-email-console-locally-gmail-to-explore). No PDFs, questions or answers are sent to Google by this sign-in flow.

Google Sign-In is for active student and teacher accounts only. **Founder and administrator accounts continue to use their existing sign-in; the Founder Dashboard still requires password and TOTP.**

The implementation has automated tests using synthetic identities and provider responses. **A real Google sign-in has not been tested in this environment.** The site owner must supply credentials and perform the controlled end-to-end test below before offering it to users.

## 1. Update safely

Back up the existing database, media and private files, and verify the backup before upgrading. Do not replace an existing `.env`, database or uploads. On the intended branch, after checking for local changes, install the requirements and migrate using your existing environment. For example, in Windows PowerShell from the project folder:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe manage.py migrate
```

On macOS/Linux use the environment's `python` in the same commands. Migration `accounts/0005` adds two tables for identity links and short-lived OAuth state; it does not reset accounts, passwords, roles, credits, attempts or timers. Rehearse upgrades on a copy first. In production, collect static files and restart the application as described in the [README](../README.md#static-files-and-troubleshooting).

## 2. Create a local test client in Google Cloud

1. Open [Google Cloud Console](https://console.cloud.google.com/), create or select your project, and configure **Google Auth Platform / OAuth consent screen**. Enter a truthful app name and support contact. For a public-facing app, choose the appropriate **External** audience; an **Internal** app only admits members of the relevant organization. Console labels and eligibility may change.
2. Use **Testing** for the initial controlled test. Add a Google account you control as a test user if the console requires it. This implementation requests only `openid`, `email` and `profile`, not Gmail, Drive or other sensitive scopes. Google's published audience rules describe special treatment for these basic scopes; confirm current restrictions and any verification requirements in your own console before public use.
3. Under **Clients / Credentials**, create an OAuth client of application type **Web application**. For a local server running at `http://127.0.0.1:8000`, add this exact **Authorized redirect URI** (including the trailing slash):

   ```text
   http://127.0.0.1:8000/auth/google/callback/
   ```

   The callback and the browser address must use the same scheme, host and port. `localhost` and `127.0.0.1` are different origins; changing either the hostname or port requires a matching `PUBLIC_BASE_URL` and registered callback. Google's [redirect rules](https://developers.google.com/identity/protocols/oauth2/web-server#redirect-uri-validation) allow loopback HTTP for local development; public deployments require HTTPS. This server-side redirect flow does not load Google's JavaScript button SDK.
4. Copy the client ID and client secret **privately** from the console. Never commit credentials, share them in chat/screenshots, or add them to `.env.example`. Keep the secret in a local `.env` excluded from Git or in your deployment secret manager.

## 3. Configure the local application

On a **separate local test copy**, open PowerShell in the project folder. If `.env` does not exist, make a private copy of the example; the conditional command below does **not** replace an existing `.env`:

```powershell
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
notepad .env
```

Edit the existing lines for these settings (do not add duplicates). For a local server opened at `http://127.0.0.1:8000`, the values are:

```text
DEBUG=True
PUBLIC_BASE_URL=http://127.0.0.1:8000
GOOGLE_SIGN_IN_ENABLED=True
GOOGLE_OAUTH_CLIENT_ID=<your-web-client-id>
GOOGLE_OAUTH_CLIENT_SECRET=<your-web-client-secret>
```

Replace the placeholders locally with the values from your Google Cloud **Web application** client. Do not post the secret or the `.env` file in chat or screenshots. Keep every other setting, especially `SECRET_KEY`, `FOUNDER_TOTP_ENCRYPTION_KEY` and the database configuration. `PUBLIC_BASE_URL` is also used for verification-email links, so use the site's actual origin; never change a live site to localhost or set `DEBUG=True` there. If your local port or hostname differs, use that exact origin here and in Google's registered callback.

Save the file and, in the same project folder, run the read-only diagnostic:

```powershell
.\.venv\Scripts\python.exe manage.py check_google_signin
```

The command lists settings that need attention and, when safe, the callback URL to register in Google Cloud. It does not print the client ID or secret, contact Google, check the database/migrations, or modify files or accounts. A nonzero exit status means the local settings are not ready. Existing process environment variables override values in `.env`; check the server's service environment if a change appears ineffective. Restart the server, then open **exactly** `http://127.0.0.1:8000/` in the browser on the same computer.

Incomplete configuration leaves the button disabled and password sign-in available. An enabled button or a successful diagnostic only means the local settings have the expected format—it does **not** prove that Google accepted the client, callback, network, or consent configuration. Apply the migration described in step 1 before starting the Google flow, and perform the controlled real-provider test below.

## 4. Controlled test

Using only accounts you own:

1. In an isolated test database, click **Continue with Google** on registration. Choose the Google account, finish the local student/teacher form, and choose JEE or NEET for a student. Confirm the appropriate dashboard and that there is exactly one site account. Sign out and sign in again with Google. No verification email is required for a verified `@gmail.com` address or a verified address at the Workspace hosted domain Google asserts.
2. For an existing password account with the **same** address, sign in using the existing password first. Open **My Profile → Google sign-in** and connect Google. If the site account is waiting for verification, the matching option is also on the verification-pending page. Sign out and test both sign-in methods. Confirm the same role, exam track, attempts, timers and credit balances remain. Account email matches never silently merge existing accounts; if you cannot sign in to the old account, use password recovery or contact a trusted site operator. Do not create a second account to regain credits.
3. Google identifies a user by stable `sub`, not by email. A Google account using a non-Google-hosted email address does **not** by itself prove current ownership of that mailbox: the site still requires its normal verification email before new work. A previously linked subject can sign in, but a changed site email remains unverified until independently confirmed. Admin/super-admin and inactive accounts cannot use this Google sign-in flow.
4. A new Google-only account has no local password. After its profile email is verified, **My Profile → Add a password** can add one without changing Google access or records. Ordinary reset emails are only available after a local password exists and mail delivery is configured. Password creation invalidates other sessions. There is currently no self-service Google disconnect/relink; use a trusted support process rather than deleting an account or records. Turning Google Sign-In off also prevents Google-only users from signing in until they have another access method.
5. Test cancellation, an expired attempt, the wrong Google account and an unavailable provider. They should not create or merge accounts. Then, separately, use the documented controlled-inbox SMTP test and check Inbox **and** Spam. Google login success does not demonstrate email delivery.

## 5. Public HTTPS deployment

Use a separate production OAuth web client and a stable public HTTPS origin. Register `https://your-domain.example/auth/google/callback/` with the exact actual domain, set `PUBLIC_BASE_URL` to `https://your-domain.example` (no path or trailing query), and configure the real domain in `ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS`. Complete Google's current branding, domain, consent-screen, privacy-policy and publication/verification requirements as applicable. Do not publish test or demo passwords. Replace the Django development secret and follow deployment security, backup, monitoring and abuse-rate-limit guidance.

When TLS terminates at a reverse proxy, set `TRUST_PROXY_HTTPS=True` **only** if that proxy strips untrusted forwarded headers and supplies the correct `X-Forwarded-Proto`; otherwise do not trust that header. Secure, same-site session and CSRF cookies must survive Google's top-level redirect, and all web workers must share the same persistent session/database and secret key. Browser access must use the configured origin. Google token and signing-key endpoints must be reachable from the server; network timeouts and provider failures fail closed.

The application uses a one-time, session-bound state, nonce, PKCE and Google's maintained verifier for signature, issuer, audience and token time checks. It stores only the linked Google subject and timestamps, not Google passwords, access tokens or refresh tokens. Expired state markers are cleaned opportunistically on later sign-in starts. There is no special scheduled cleanup or provider uptime guarantee. Protect session data and database backups. The callback URL carries a short-lived authorization code and state in its query string: configure application/server/reverse-proxy access logs **not** to record callback query strings, restrict logs and never post them publicly. The callback response uses `Referrer-Policy: no-referrer`; onboarding/password forms use `same-origin` so HTTPS CSRF checks work without sending referrers to third parties.

If Google reports `redirect_uri_mismatch`, compare the registered callback with the actual configured scheme, hostname, port, path and trailing slash. If the site reports an origin or session error, open the configured address directly, allow cookies, check the HTTPS proxy configuration and restart the flow. If Google refuses a user, check the current audience/testing/consent rules. Do not debug by sharing secrets, callback URLs, tokens, cookies or personal account data.
