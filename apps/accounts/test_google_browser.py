"""Optional real-Chromium HTTPS workflow, with a synthetic Google provider.

A local TLS proxy exercises secure cookies and real browser CSRF behavior. The
provider redirect and code exchange are stubbed; no Google account is contacted.
"""
import http.client
import ipaddress
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
from datetime import datetime, timedelta, timezone as dt_timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import skipUnless
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from apps.exams.models import Category, Exam, ExamAttempt
from apps.payments.models import CreditUse
from .google_signin import GoogleSignInError
from .models import GoogleIdentity, User
from .test_google_signin import CLIENT_ID, CLIENT_SECRET


class LocalTLSProxy:
    """Temporary HTTPS-only frontend to Django's disposable HTTP live server."""
    def __init__(self, backend_url, folder):
        backend = urlsplit(backend_url)

        class Proxy(BaseHTTPRequestHandler):
            def do_GET(self):
                parts = urlsplit(self.path)
                if parts.path == "/synthetic-google/authorize/" and self.headers.get("Host", "").startswith("localhost:"):
                    query = parse_qs(parts.query)
                    origin = f"https://127.0.0.1:{self.server.server_port}"
                    redirect = origin + reverse("accounts:google_callback")
                    allowed = {"existing", "pending", "new-student", "new-teacher"}
                    if (query.get("client_id") != [CLIENT_ID] or query.get("redirect_uri") != [redirect]
                            or query.get("code_challenge_method") != ["S256"] or not query.get("code_challenge")
                            or not query.get("state") or not query.get("nonce")
                            or query.get("synthetic_identity", [""])[0] not in allowed):
                        self.send_error(400, "Invalid synthetic authorization request")
                        return
                    callback = redirect + "?" + urlencode({"state": query["state"][0], "code": query["synthetic_identity"][0]})
                    self.send_response(302)
                    self.send_header("Location", callback)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.forward()

            def do_POST(self):
                self.forward()

            def do_HEAD(self):
                self.forward()

            def log_message(self, *_args):
                pass  # Never write synthetic callback query strings to logs.

            def forward(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                headers = {name: value for name, value in self.headers.items()
                           if name.lower() not in ("connection", "transfer-encoding", "x-forwarded-proto")}
                headers["X-Forwarded-Proto"] = "https"
                connection = http.client.HTTPConnection(backend.hostname, backend.port, timeout=30)
                try:
                    connection.request(self.command, self.path, body=body, headers=headers)
                    response = connection.getresponse()
                    content = response.read()
                    self.send_response(response.status)
                    for name, value in response.getheaders():
                        if name.lower() == "location" and value.startswith("https://accounts.google.com/o/oauth2/v2/auth?"):
                            # Simulate Google's redirect on another loopback host;
                            # no browser or server request leaves this machine.
                            query = urlsplit(value).query
                            identity = self.headers.get("X-Synthetic-Identity", "")
                            value = (f"https://localhost:{self.server.server_port}/synthetic-google/authorize/?" + query
                                     + "&" + urlencode({"synthetic_identity": identity}))
                        if name.lower() not in ("connection", "transfer-encoding", "server", "date", "content-length"):
                            self.send_header(name, value)
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    if self.command != "HEAD":
                        self.wfile.write(content)
                except (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError):
                    # Browsers may cancel a static request when navigating.
                    pass
                finally:
                    connection.close()

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
        now = datetime.now(dt_timezone.utc)
        certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                       .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(minutes=1))
                       .not_valid_after(now + timedelta(days=1))
                       .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                                                                    x509.DNSName("localhost")]), False)
                       .sign(key, hashes.SHA256()))
        cert_path, key_path = Path(folder) / "cert.pem", Path(folder) / "key.pem"
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                               serialization.NoEncryption()))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert_path, key_path)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"https://127.0.0.1:{self.server.server_port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Optional Chromium HTTPS Google UI workflow")
@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class GoogleSignInBrowserTests(StaticLiveServerTestCase):
    def test_secure_browser_google_onboarding_linking_and_password_fallback(self):
        if not shutil.which("node") or not os.environ.get("CHROMIUM_PATH"):
            self.fail("Set CHROMIUM_PATH and install tests/browser dependencies first.")
        password = "ExistingPassword987!"
        existing = User.objects.create_user("browser-existing", "existing@gmail.com", password, role="STUDENT",
                                            exam_track="jee", verification_required=True, is_verified=True)
        pending = User.objects.create_user("browser-pending", "pending@gmail.com", password, role="TEACHER",
                                           verification_required=True, is_verified=False)
        category = Category.objects.create(name="Google browser", slug="google-browser")
        exam = Exam.objects.create(title="Preserved browser test", slug="google-browser-exam", category=category,
                                   creator=pending, status="published", target_type="jee")
        deadline = timezone.now() + timedelta(minutes=25)
        attempt = ExamAttempt.objects.create(exam=exam, student=existing, status="in_progress", expires_at=deadline)
        use = CreditUse.objects.create(user=existing, kind="classroom", is_free=True, resource_key="google-browser-use")
        profiles = {
            "existing": {"subject": "browser-existing-sub", "email": existing.email, "first_name": "Existing", "last_name": "Student", "authoritative": True},
            "pending": {"subject": "browser-pending-sub", "email": pending.email, "first_name": "Pending", "last_name": "Teacher", "authoritative": True},
            "new-student": {"subject": "browser-new-student-sub", "email": "newstudent@gmail.com", "first_name": "New", "last_name": "Student", "authoritative": True},
            "new-teacher": {"subject": "browser-new-teacher-sub", "email": "newteacher@gmail.com", "first_name": "New", "last_name": "Teacher", "authoritative": True},
        }

        def synthetic_exchange(code, _flow, _config):
            if code not in profiles:
                raise GoogleSignInError("Synthetic test provider rejected this request.")
            return profiles[code]

        with tempfile.TemporaryDirectory(prefix="google-browser-tls-") as folder:
            proxy = LocalTLSProxy(self.live_server_url, folder)
            self.addCleanup(proxy.close)
            data = {"base": proxy.origin, "client_id": CLIENT_ID, "password": password,
                    "existing": existing.username, "pending": pending.username,
                    "login": reverse("accounts:login"), "register": reverse("accounts:register"),
                    "profile": reverse("accounts:profile"), "logout": reverse("accounts:logout"),
                    "callback": reverse("accounts:google_callback"), "complete": reverse("accounts:google_signup"),
                    "add_password": reverse("accounts:google_add_password")}
            env = os.environ.copy()
            env["GOOGLE_BROWSER_DATA"] = json.dumps(data)
            script = Path(settings.BASE_DIR) / "tests" / "browser" / "google-signin-smoke.cjs"
            with override_settings(GOOGLE_SIGN_IN_ENABLED=True, GOOGLE_OAUTH_CLIENT_ID=CLIENT_ID,
                    GOOGLE_OAUTH_CLIENT_SECRET=CLIENT_SECRET, PUBLIC_BASE_URL=proxy.origin,
                    ALLOWED_HOSTS=["127.0.0.1", "localhost", "testserver"],
                    CSRF_TRUSTED_ORIGINS=[proxy.origin], SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
                    SESSION_COOKIE_SECURE=True, CSRF_COOKIE_SECURE=True), \
                 patch("apps.accounts.google_views.exchange_code", side_effect=synthetic_exchange):
                result = subprocess.run(["node", str(script)], env=env, text=True, capture_output=True, timeout=180)
            self.assertEqual(result.returncode, 0, f"Google browser workflow failed:\n{result.stdout}\n{result.stderr}")
            self.assertIn("GOOGLE_BROWSER_OK", result.stdout)
        existing.refresh_from_db(); pending.refresh_from_db(); attempt.refresh_from_db(); use.refresh_from_db()
        self.assertTrue(existing.check_password(password))
        self.assertTrue(pending.check_password(password))
        self.assertTrue(pending.is_verified)
        self.assertEqual((existing.role, existing.exam_track, attempt.expires_at, attempt.status, use.user_id),
                         ("STUDENT", "jee", deadline, "in_progress", existing.pk))
        self.assertEqual(GoogleIdentity.objects.get(subject="browser-existing-sub").user_id, existing.pk)
        self.assertEqual(GoogleIdentity.objects.get(subject="browser-pending-sub").user_id, pending.pk)
        student = User.objects.get(email="newstudent@gmail.com")
        teacher = User.objects.get(email="newteacher@gmail.com")
        self.assertEqual((student.role, student.exam_track, teacher.role, teacher.exam_track), ("STUDENT", "neet", "TEACHER", ""))
        self.assertTrue(student.check_password("NewBrowserPassword987!"))
        self.assertFalse(teacher.has_usable_password())
        self.assertEqual(User.objects.count(), 4)
        self.assertEqual(GoogleIdentity.objects.count(), 4)
