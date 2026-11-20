"""Expose only whether Google Sign-In is configured, never its secret."""


def google_sign_in_context(request):
    from .google_signin import configuration
    return {"GOOGLE_SIGN_IN_READY": bool(configuration())}
