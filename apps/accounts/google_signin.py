"""Google OpenID Connect helpers. No Gmail access or long-lived token storage.

The browser is redirected to Google using a server-generated state, nonce and
PKCE challenge. The authorization code is exchanged server-side and the
Google ID token is verified with Google's maintained verifier. All provider
failures fail closed, and neither codes nor credentials are logged.
"""
import base64
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta
from urllib.parse import urlencode, urlsplit

import requests
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.urls import reverse
from django.utils import timezone
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token as google_id_token

from .models import GoogleOAuthState
from .verification import base_url

logger = logging.getLogger(__name__)
AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
FLOW_SESSION_KEY = "google_sign_in_flow"
SIGNUP_SESSION_KEY = "google_sign_in_signup"
FLOW_LIFETIME = timedelta(minutes=10)
SIGNUP_LIFETIME_SECONDS = 15 * 60


class GoogleSignInError(Exception):
    """A safe, non-sensitive error suitable for a user-facing message."""


def configuration_status():
    """Return the valid origin and issue codes, without the credential fields.

    The same checks drive both the button and the operator diagnostic. The
    origin is taken from settings, not an incoming Host header. HTTP is only
    permitted for a loopback development origin.
    """
    issues = []
    if not settings.GOOGLE_SIGN_IN_ENABLED:
        issues.append("disabled")
    client_id = settings.GOOGLE_OAUTH_CLIENT_ID
    client_secret = settings.GOOGLE_OAUTH_CLIENT_SECRET
    if (not isinstance(client_id, str) or not client_id.endswith(".apps.googleusercontent.com")
            or not 1 <= len(client_id) <= 255 or any(c.isspace() for c in client_id)):
        issues.append("client_id")
    if (not isinstance(client_secret, str) or not 1 <= len(client_secret) <= 1024
            or any(c.isspace() for c in client_secret)):
        issues.append("client_secret")
    origin = base_url() if isinstance(settings.PUBLIC_BASE_URL, str) else ""
    if origin:
        parts = urlsplit(origin)
        if (any(c.isspace() or not c.isprintable() for c in origin)
                or (parts.scheme == "http" and parts.hostname not in ("localhost", "127.0.0.1", "::1"))):
            origin = ""
    if not origin:
        issues.append("origin")
    return origin, tuple(issues)


def configuration():
    """Return trusted settings only when the entire OAuth setup is present."""
    origin, issues = configuration_status()
    if issues:
        return None
    return {
        "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
        "origin": origin,
        "redirect_uri": origin + reverse("accounts:google_callback"),
    }


def _origin_tuple(parts):
    try:
        return (parts.scheme.lower(), (parts.hostname or "").lower(),
                parts.port or (443 if parts.scheme.lower() == "https" else 80))
    except ValueError:
        return None


def same_configured_origin(request, config):
    """A host-only session cookie must survive the round trip to the callback."""
    current = urlsplit(f"{request.scheme}://{request.get_host()}")
    return _origin_tuple(current) == _origin_tuple(urlsplit(config["origin"]))


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _prune_expired_states(now):
    # Keep cleanup bounded and never touch user/attempt/billing records.
    old = list(GoogleOAuthState.objects.filter(expires_at__lt=now)
               .order_by("expires_at").values_list("digest", flat=True)[:100])
    if old:
        GoogleOAuthState.objects.filter(digest__in=old, expires_at__lt=now).delete()


def create_authorization_url(request, config, *, mode, next_url=""):
    """Make one session-bound, ten-minute authorization request."""
    now = timezone.now()
    _prune_expired_states(now)
    previous = request.session.get(FLOW_SESSION_KEY)
    if isinstance(previous, dict) and isinstance(previous.get("digest"), str):
        GoogleOAuthState.objects.filter(digest=previous["digest"], consumed_at__isnull=True).update(consumed_at=now)
    request.session.pop(SIGNUP_SESSION_KEY, None)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)  # 86 URL-safe characters, within PKCE's 43–128 range.
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    digest = _digest(state)
    GoogleOAuthState.objects.create(digest=digest, expires_at=now + FLOW_LIFETIME)
    request.session[FLOW_SESSION_KEY] = {
        "digest": digest,
        "nonce": nonce,
        "verifier": verifier,
        "mode": mode,
        "user_id": request.user.pk if mode == "link" else None,
        "next": next_url,
    }
    params = {
        "client_id": config["client_id"],
        "redirect_uri": config["redirect_uri"],
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }
    return AUTHORIZE_URL + "?" + urlencode(params)


def consume_authorization_state(request, state):
    """Check the browser session, then atomically consume state across workers."""
    flow = request.session.get(FLOW_SESSION_KEY)
    if (not isinstance(flow, dict) or not isinstance(state, str) or not 1 <= len(state) <= 128
            or not state.isascii() or not isinstance(flow.get("digest"), str)):
        return None
    digest = _digest(state)
    if not hmac.compare_digest(digest, flow["digest"]):
        return None
    request.session.pop(FLOW_SESSION_KEY, None)
    now = timezone.now()
    used = GoogleOAuthState.objects.filter(digest=digest, consumed_at__isnull=True,
                                            expires_at__gt=now).update(consumed_at=now)
    return flow if used == 1 else None


def _verify_with_google_keys(raw_token, client_id):
    """Verify Google's signature, issuer, audience, expiry and issued-at.

    Google's cert endpoint is fixed by the official library, not by token data.
    Bound network time and disallow redirects for the certificate fetch.
    """
    with requests.Session() as session:
        transport = GoogleRequest(session=session)

        def bounded(url, method="GET", body=None, headers=None, **kwargs):
            return transport(url, method=method, body=body, headers=headers,
                             timeout=(3.05, 8), allow_redirects=False)

        return google_id_token.verify_oauth2_token(raw_token, bounded, audience=client_id,
                                                    clock_skew_in_seconds=30)


def _verified_profile(raw_token, config, expected_nonce):
    if not isinstance(raw_token, str) or not 1 <= len(raw_token) <= 16384:
        raise GoogleSignInError("Google could not confirm this sign-in. Please try again.")
    try:
        claims = _verify_with_google_keys(raw_token, config["client_id"])
        if not isinstance(claims, dict):
            raise ValueError("Invalid claims")
        # Defense in depth alongside the signature/audience/issuer checks in
        # google-auth. Never accept a token intended for a different client.
        if (claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com")
                or claims.get("aud") != config["client_id"]
                or (claims.get("azp") is not None and claims.get("azp") != config["client_id"])):
            raise ValueError("Invalid issuer or audience")
        nonce = claims.get("nonce")
        if (not isinstance(nonce, str) or not isinstance(expected_nonce, str)
                or not nonce.isascii() or not expected_nonce.isascii()
                or not hmac.compare_digest(nonce, expected_nonce)):
            raise ValueError("Invalid nonce")
        subject = claims.get("sub")
        if (not isinstance(subject, str) or not 1 <= len(subject) <= 255
                or not subject.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in subject)):
            raise ValueError("Invalid subject")
        email = claims.get("email")
        if not isinstance(email, str) or len(email) > 254 or claims.get("email_verified") is not True:
            raise ValueError("Unverified email")
        email = email.strip().lower()
        validate_email(email)
    except (ValueError, TypeError, KeyError, ValidationError, google_auth_exceptions.GoogleAuthError,
            requests.RequestException, OSError):
        # No exception details, token, email or nonce in logs.
        logger.warning("Google ID token could not be verified.")
        raise GoogleSignInError("Google could not confirm this sign-in. Please try again.") from None

    domain = email.rsplit("@", 1)[-1]
    hosted_domain = claims.get("hd")
    # Google is authoritative for @gmail.com and verified addresses at the
    # hosted Workspace domain, not arbitrary third-party account addresses.
    authoritative = domain == "gmail.com" or (
        isinstance(hosted_domain, str) and bool(hosted_domain) and hosted_domain.lower() == domain
    )

    def clean_name(value):
        return value.strip()[:150] if isinstance(value, str) else ""

    return {
        "subject": subject,
        "email": email,
        "first_name": clean_name(claims.get("given_name")),
        "last_name": clean_name(claims.get("family_name")),
        "authoritative": authoritative,
    }


def exchange_code(code, flow, config):
    """Exchange one code and return only verified, minimal profile claims."""
    if (not isinstance(code, str) or not 1 <= len(code) <= 4096
            or not code.isascii() or any(ord(c) < 33 or ord(c) > 126 for c in code)
            or not isinstance(flow.get("verifier"), str)):
        raise GoogleSignInError("Google sign-in could not be completed. Please try again.")
    try:
        response = requests.post(TOKEN_URL, data={
            "code": code,
            "client_id": config["client_id"],
            "client_secret": config["client_secret"],
            "redirect_uri": config["redirect_uri"],
            "grant_type": "authorization_code",
            "code_verifier": flow["verifier"],
        }, timeout=(3.05, 8), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError("Token exchange was not accepted")
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Invalid token response")
    except (requests.RequestException, ValueError, TypeError):
        logger.warning("Google authorization code exchange did not complete.")
        raise GoogleSignInError("Google sign-in is temporarily unavailable. Please try again or use your password.") from None
    return _verified_profile(body.get("id_token"), config, flow.get("nonce"))
