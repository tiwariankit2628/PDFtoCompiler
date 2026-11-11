"""Isolated settings for optional Chromium and concurrent live-server tests.

Django shares one SQLite connection between threads for an in-memory test
DB. Real browsers issue overlapping requests, so use a unique disposable
file-backed test DB instead. Normal application and production settings are
unchanged; the Django test runner removes this file after the run.
"""
from copy import deepcopy
from pathlib import Path
from tempfile import gettempdir
from uuid import uuid4

from .settings import *  # noqa: F401,F403

DATABASES = deepcopy(DATABASES)  # noqa: F405
if DATABASES["default"]["ENGINE"] == "django.db.backends.sqlite3":
    test_database = Path(gettempdir()) / f"pdftocompiler-test-{uuid4().hex}.sqlite3"
    DATABASES["default"]["TEST"] = {**DATABASES["default"].get("TEST", {}), "NAME": str(test_database)}
