"""The synthetic benchmark must fail closed before touching existing data."""
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase
from django.urls import reverse

from tools import loadtest_classroom as tool


class CapacityToolSafetyTests(SimpleTestCase):
    model_names = ("User", "Exam", "Question", "QuestionPaperUpload", "ClassroomSession",
                   "ExamAttempt", "PackCheckout", "CreditUse", "CreditGrant")

    def setUp(self):
        self.config = SimpleNamespace(DEBUG=True, DATABASES={"default": {"NAME": "loadtest_fresh", "HOST": "/tmp/pg"}},
            ALLOW_TEST_PAYMENT_CREDITS=False, RAZORPAY_KEY_ID="", RAZORPAY_KEY_SECRET="", RAZORPAY_WEBHOOK_SECRET="",
            EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
        self.connection = SimpleNamespace(vendor="postgresql")
        self.args = SimpleNamespace(base_url="http://127.0.0.1:8010", users=40, concurrency=40,
                                    questions=30, saves=3, i_understand_this_creates_synthetic_records=True)
        self.models = {name: type(name, (), {"objects": SimpleNamespace(exists=Mock(return_value=False))})
                       for name in self.model_names}
        self.patchers = [patch.object(tool, "settings", self.config), patch.object(tool, "connection", self.connection),
                         patch.multiple(tool, **self.models)]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_fresh_local_disposable_configuration_passes(self):
        tool.check_safety(self.args)
        for model in self.models.values():
            model.objects.exists.assert_called_once_with()

    def test_nonlocal_or_ambiguous_targets_never_query_database(self):
        for url in ("https://localhost:8010", "http://example.com:8010", "http://127.0.0.1:8010/path",
                    "http://127.0.0.1:8010?x=1", "http://127.0.0.1", "http://user@localhost:8010"):
            with self.subTest(url=url):
                self.args.base_url = url
                with self.assertRaises(ValueError):
                    tool.check_safety(self.args)
        for model in self.models.values():
            model.objects.exists.assert_not_called()

    def test_requires_acknowledgment_debug_local_postgres_and_disposable_name(self):
        changes = [(self.args, "i_understand_this_creates_synthetic_records", False),
                   (self.config, "DEBUG", False), (self.connection, "vendor", "sqlite")]
        for target, name, value in changes:
            with self.subTest(name=name), patch.object(target, name, value), self.assertRaises(ValueError):
                tool.check_safety(self.args)
        for db in ({"NAME": "production", "HOST": "localhost"},
                   {"NAME": "loadtest_fresh", "HOST": "remote.example"},
                   {"NAME": "loadtest_fresh", "HOST": ""}):
            with self.subTest(database=db), patch.object(self.config, "DATABASES", {"default": db}), self.assertRaises(ValueError):
                tool.check_safety(self.args)

    def test_provider_credentials_or_real_mail_backend_are_rejected(self):
        for name, value in (("ALLOW_TEST_PAYMENT_CREDITS", True), ("RAZORPAY_KEY_ID", "synthetic-key"),
                            ("RAZORPAY_KEY_SECRET", "synthetic-secret"), ("RAZORPAY_WEBHOOK_SECRET", "synthetic-secret"),
                            ("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend")):
            with self.subTest(name=name), patch.object(self.config, name, value), self.assertRaises(ValueError):
                tool.check_safety(self.args)
        for model in self.models.values():
            model.objects.exists.assert_not_called()

    def test_existing_user_data_and_out_of_bounds_sizes_are_rejected(self):
        self.models["Question"].objects.exists.return_value = True
        with self.assertRaisesRegex(ValueError, "not empty"):
            tool.check_safety(self.args)
        self.models["Question"].objects.exists.return_value = False
        for name, value in (("users", 0), ("users", 501), ("concurrency", 41),
                            ("questions", 201), ("saves", 0), ("saves", 31)):
            with self.subTest(name=name, value=value), patch.object(self.args, name, value), self.assertRaises(ValueError):
                tool.check_safety(self.args)

    def test_server_must_recognize_this_runs_synthetic_session(self):
        http = Mock()
        client = {"username": "load_random_marker_0001", "http": http}
        http.get.return_value = SimpleNamespace(status_code=200, content=b"Hello load_random_marker_0001")
        tool.check_server_session("http://localhost:8010", client)
        http.get.assert_called_with("http://localhost:8010" + reverse("accounts:profile"), timeout=15, allow_redirects=False)
        for response in (SimpleNamespace(status_code=302, content=b""), SimpleNamespace(status_code=200, content=b"other user")):
            http.get.return_value = response
            with self.assertRaises(ValueError):
                tool.check_server_session("http://localhost:8010", client)
        http.get.side_effect = requests.ConnectionError("unavailable")
        with self.assertRaisesRegex(ValueError, "ConnectionError"):
            tool.check_server_session("http://localhost:8010", client)
