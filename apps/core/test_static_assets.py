"""Static assets must remain deployable with Django's manifest storage."""
from html.parser import HTMLParser
from pathlib import Path
import re
from tempfile import TemporaryDirectory

from django.conf import settings
from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse


class LocalAssetParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        url = attrs.get("href" if tag == "link" else "src", "")
        if tag in ("link", "script") and url.startswith(settings.STATIC_URL):
            self.assets.append((url, attrs))


class StaticAssetTests(TestCase):
    def test_development_finders_locate_first_party_assets(self):
        for path in (
            "css/style.css",
            "js/main.js",
            "vendor/bootstrap/bootstrap.min.css",
            "vendor/bootstrap/bootstrap.bundle.min.js",
            "vendor/bootstrap-icons/fonts/bootstrap-icons.woff2",
            "vendor/katex/katex.min.css",
            "vendor/chartjs/chart.umd.min.js",
        ):
            with self.subTest(path=path):
                self.assertTrue(finders.find(path), f"Static file is not discoverable: {path}")

    def test_bundled_assets_do_not_use_precollection_sri(self):
        # Audit every template, including the KaTeX pages not rendered by home.
        for path in (Path(settings.BASE_DIR) / "templates").rglob("*.html"):
            for tag in re.findall(r"<(?:link|script)\b[^>]*>", path.read_text(encoding="utf-8"), re.I | re.S):
                if "{% static " in tag:
                    with self.subTest(template=str(path), tag=tag):
                        self.assertNotRegex(tag, r"\bintegrity\s*=")

    def test_production_collectstatic_and_whitenoise_serve_processed_assets(self):
        """Missing referenced files and stale pre-collection SRI must fail here."""
        with TemporaryDirectory(prefix="static-collection-test-") as root:
            with override_settings(
                DEBUG=False,
                ALLOWED_HOSTS=["testserver"],
                SECURE_SSL_REDIRECT=False,
                STATIC_ROOT=root,
                WHITENOISE_AUTOREFRESH=False,
                WHITENOISE_USE_FINDERS=False,
                STORAGES={
                    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
                },
            ):
                call_command("collectstatic", interactive=False, verbosity=0)
                self.assertTrue((Path(root) / "staticfiles.json").is_file())
                html = self.client.get(reverse("core:home"))
                self.assertEqual(html.status_code, 200)
                parser = LocalAssetParser()
                parser.feed(html.content.decode())
                self.assertTrue(any("bootstrap.min." in url for url, _ in parser.assets))
                self.assertTrue(any("bootstrap.bundle.min." in url for url, _ in parser.assets))
                for url, attrs in parser.assets:
                    with self.subTest(url=url):
                        # The upstream files are modified by manifest post-processing;
                        # an SRI hash calculated before collection blocks them.
                        self.assertNotIn("integrity", attrs)
                        response = self.client.get(url)
                        self.assertEqual(response.status_code, 200)
                        response.close()

                for name in (
                    "vendor/bootstrap/bootstrap.min.css.map",
                    "vendor/bootstrap/bootstrap.bundle.min.js.map",
                    "vendor/chartjs/chart.umd.js.map",
                    "vendor/chartjs/chart.umd.min.js",
                    "vendor/katex/katex.min.css",
                    "vendor/katex/katex.min.js",
                    "vendor/katex/fonts/KaTeX_Main-Regular.woff",
                    "vendor/katex/fonts/KaTeX_Main-Regular.ttf",
                ):
                    with self.subTest(name=name):
                        url = staticfiles_storage.url(name)
                        response = self.client.get(url)
                        self.assertEqual(response.status_code, 200, f"Missing collected file: {url}")
                        response.close()
