"""
Django settings for CBT Examination Platform.
Configured for commercial-grade scalability, security, and modularity.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# Load environment variables from .env file
load_dotenv(BASE_DIR / ".env")

# ------------------------------------------------------------------------------
# CORE SECURITY & ENVIRONMENT
# ------------------------------------------------------------------------------
SECRET_KEY = os.environ.get(
    "SECRET_KEY",
    "django-insecure-cbt-default-dev-key-change-in-production-random-hash-98213"
)

DEBUG = os.environ.get("DEBUG", "True").lower() in ("true", "1", "yes")

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1,0.0.0.0").split(",")
    if host.strip()
]
if DEBUG:
    for dev_host in ["testserver", "localhost", "127.0.0.1", "[::1]"]:
        if dev_host not in ALLOWED_HOSTS:
            ALLOWED_HOSTS.append(dev_host)

# Configure trusted origins explicitly when behind a preview/reverse proxy.
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]
if os.environ.get("TRUST_PROXY_HTTPS", "False").lower() in ("true", "1", "yes"):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# ------------------------------------------------------------------------------
# PLATFORM BRANDING & CONFIGURATION
# ------------------------------------------------------------------------------
PLATFORM_NAME = os.environ.get("PLATFORM_NAME", "TestForge CBT")
PLATFORM_TAGLINE = os.environ.get(
    "PLATFORM_TAGLINE",
    "Commercial-Grade Online Examination & Assessment Engine"
)
PLATFORM_SUPPORT_EMAIL = os.environ.get("PLATFORM_SUPPORT_EMAIL", "support@testforge.local")

# ------------------------------------------------------------------------------
# APPLICATION DEFINITION
# ------------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
]

THIRD_PARTY_APPS = [
    # Any third-party Django packages
]

LOCAL_APPS = [
    "apps.core.apps.CoreConfig",
    "apps.accounts.apps.AccountsConfig",
    "apps.exams.apps.ExamsConfig",
    "apps.questions.apps.QuestionsConfig",
    "apps.payments.apps.PaymentsConfig",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.platform_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# ------------------------------------------------------------------------------
# DATABASE CONFIGURATION
# ------------------------------------------------------------------------------
# Safe dual-mode database engine:
# - SQLite for seamless zero-setup local dev and automated test suites
# - PostgreSQL via psycopg (v3) for production or when DB_ENGINE=postgres
DB_ENGINE_CHOICE = os.environ.get("DB_ENGINE", "sqlite").lower()

if DB_ENGINE_CHOICE in ("postgres", "postgresql"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("DB_NAME", "testgen_cbt"),
            "USER": os.environ.get("DB_USER", "postgres"),
            "PASSWORD": os.environ.get("DB_PASSWORD", "postgres"),
            "HOST": os.environ.get("DB_HOST", "127.0.0.1"),
            "PORT": os.environ.get("DB_PORT", "5432"),
            "CONN_MAX_AGE": 60,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
        }
    }

# ------------------------------------------------------------------------------
# CUSTOM USER MODEL & AUTHENTICATION
# ------------------------------------------------------------------------------
AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard_router"
LOGOUT_REDIRECT_URL = "core:home"

# ------------------------------------------------------------------------------
# INTERNATIONALIZATION & TIME
# ------------------------------------------------------------------------------
LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# ------------------------------------------------------------------------------
# STATIC & MEDIA FILES
# ------------------------------------------------------------------------------
STATIC_URL = "/static/"
# Django's development staticfiles handler uses these finders; WhiteNoise serves
# the collected manifest from STATIC_ROOT in production.
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"
        if not DEBUG
        else "django.contrib.staticfiles.storage.StaticFilesStorage",
    },
}

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

# Student PDFs and crops are never exposed through MEDIA_URL. Keep this folder
# private in your web server and back it up as sensitive user data.
PRIVATE_PRACTICE_ROOT = os.environ.get("PRIVATE_PRACTICE_ROOT", str(BASE_DIR / "private_practice"))
PRACTICE_MIN_DURATION_MINUTES = int(os.environ.get("PRACTICE_MIN_DURATION_MINUTES", "5"))
PRACTICE_MAX_DURATION_MINUTES = int(os.environ.get("PRACTICE_MAX_DURATION_MINUTES", "360"))
PRACTICE_MAX_PDF_PAGES = int(os.environ.get("PRACTICE_MAX_PDF_PAGES", "100"))

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ------------------------------------------------------------------------------
# CELERY & REDIS
# ------------------------------------------------------------------------------
CELERY_ALWAYS_EAGER = os.environ.get("CELERY_ALWAYS_EAGER", "True").lower() in ("true", "1")
# Celery's namespaced setting is TASK_ALWAYS_EAGER, not ALWAYS_EAGER.
CELERY_TASK_ALWAYS_EAGER = CELERY_ALWAYS_EAGER
CELERY_BROKER_URL = os.environ.get("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.environ.get("CELERY_RESULT_BACKEND", "redis://127.0.0.1:6379/0")
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
# Requires a running Celery worker AND beat; web requests also enforce expiry.
CELERY_BEAT_SCHEDULE = {
    "expire-private-practice-attempts": {
        "task": "apps.exams.tasks.expire_private_practice_attempts",
        "schedule": 30.0,
    },
}

# ------------------------------------------------------------------------------
# OCR & PDF PROCESSING
# ------------------------------------------------------------------------------
TESSERACT_CMD = os.environ.get("TESSERACT_CMD", "")

# Optional, explicitly requested AI comparisons for completed private tests.
# The server must be a local-only Ollama instance; no model is bundled.
LOCAL_PRACTICE_AI_ENABLED = os.environ.get("LOCAL_PRACTICE_AI_ENABLED", "False").lower() in ("true", "1")
LOCAL_PRACTICE_AI_MODEL = os.environ.get("LOCAL_PRACTICE_AI_MODEL", "").strip()
LOCAL_PRACTICE_AI_PORT = os.environ.get("LOCAL_PRACTICE_AI_PORT", "11434")
LOCAL_PRACTICE_AI_TIMEOUT = os.environ.get("LOCAL_PRACTICE_AI_TIMEOUT", "120")
LOCAL_PRACTICE_AI_MAX_QUESTIONS = os.environ.get("LOCAL_PRACTICE_AI_MAX_QUESTIONS", "300")

# ------------------------------------------------------------------------------
# RAZORPAY CONFIGURATION
# ------------------------------------------------------------------------------
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")

# ------------------------------------------------------------------------------
# EMAIL CONFIGURATION
# ------------------------------------------------------------------------------
EMAIL_BACKEND = os.environ.get(
    "EMAIL_BACKEND",
    "django.core.mail.backends.console.EmailBackend"
)
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", f"noreply@{PLATFORM_NAME.lower().replace(' ', '')}.com")

# ------------------------------------------------------------------------------
# SECURITY ENHANCEMENTS
# ------------------------------------------------------------------------------
if not DEBUG:
    SECURE_BROWSER_XSS_FILTER = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True


# Optional cloud PDF extraction. Each mode is separately disabled by default.
PDF_CLOUD_ENABLED = os.environ.get("PDF_CLOUD_ENABLED", "False").lower() in ("true", "1", "yes")
PDF_GEMINI_ENABLED = os.environ.get("PDF_GEMINI_ENABLED", "False").lower() in ("true", "1", "yes")
MATHPIX_APP_ID = os.environ.get("MATHPIX_APP_ID", "")
MATHPIX_APP_KEY = os.environ.get("MATHPIX_APP_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip()
# Separate model: enabling a low-cost mode must not change the Mathpix pipeline.
PDF_GEMINI_MODEL = os.environ.get("PDF_GEMINI_MODEL", "").strip()
# Separate generation settings let a budget model coexist with a premium one.
PDF_GEMINI_THINKING_LEVEL = os.environ.get("PDF_GEMINI_THINKING_LEVEL", "").strip().lower()
PDF_GEMINI_MAX_OUTPUT_TOKENS = int(os.environ.get("PDF_GEMINI_MAX_OUTPUT_TOKENS", "32768"))
# Optional per-job pacing for rate-limited accounts; not a global rate limit.
PDF_GEMINI_MIN_INTERVAL_SECONDS = float(os.environ.get("PDF_GEMINI_MIN_INTERVAL_SECONDS", "0"))
PDF_CLOUD_MAX_PAGES = int(os.environ.get("PDF_CLOUD_MAX_PAGES", "50"))
PDF_CLOUD_POLL_SECONDS = float(os.environ.get("PDF_CLOUD_POLL_SECONDS", "3"))
PDF_CLOUD_TIMEOUT_SECONDS = int(os.environ.get("PDF_CLOUD_TIMEOUT_SECONDS", "900"))
PDF_CLOUD_MAX_OUTPUT_TOKENS = int(os.environ.get("PDF_CLOUD_MAX_OUTPUT_TOKENS", "32768"))
PDF_CLOUD_THINKING_LEVEL = os.environ.get("PDF_CLOUD_THINKING_LEVEL", "").strip().lower()

# Optional Gemini answer suggestions. Separate consent is required per request.
AI_ANSWER_ENABLED = os.environ.get("AI_ANSWER_ENABLED", "False").lower() in ("true", "1", "yes")
AI_ANSWER_MODEL = os.environ.get("AI_ANSWER_MODEL", "").strip()
AI_ANSWER_MIN_CONFIDENCE = float(os.environ.get("AI_ANSWER_MIN_CONFIDENCE", "0.70"))
AI_ANSWER_MAX_OUTPUT_TOKENS = int(os.environ.get("AI_ANSWER_MAX_OUTPUT_TOKENS", "32768"))
AI_ANSWER_THINKING_LEVEL = os.environ.get("AI_ANSWER_THINKING_LEVEL", "").strip().lower()
# Optional operator-maintained estimates; blank means cost is unknown, not free.
AI_INPUT_USD_PER_MILLION = os.environ.get("AI_INPUT_USD_PER_MILLION", "")
AI_OUTPUT_USD_PER_MILLION = os.environ.get("AI_OUTPUT_USD_PER_MILLION", "")
