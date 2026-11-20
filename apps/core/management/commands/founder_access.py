"""Explicit server-side founder enrollment/revocation; never changes a role."""
import pyotp
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from apps.accounts.models import User
from apps.core.founder_security import (FounderConfigurationError, encrypt_secret,
                                         founder_audit, founder_role_eligible, new_access_version)
from apps.core.models import FounderAccess
from apps.payments.services import lock_practice_user


class Command(BaseCommand):
    help = "Explicitly grant or revoke founder access for one existing super-administrator."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("grant", "revoke"))
        parser.add_argument("--username", required=True, help="Exact existing username, not an email address.")
        parser.add_argument("--confirm", action="store_true", help="Confirm this deliberate access change.")

    def handle(self, *args, **options):
        if not options["confirm"]:
            raise CommandError("Nothing changed. Pass --confirm after verifying the username and environment.")
        try:
            initial = User.objects.get(username=options["username"])
        except User.DoesNotExist:
            raise CommandError("No account with that exact username exists. Nothing was changed.") from None

        secret = pyotp.random_base32() if options["action"] == "grant" else None
        try:
            encrypted = encrypt_secret(secret) if secret else ""
        except FounderConfigurationError:
            raise CommandError("Configure a valid, independent FOUNDER_TOTP_ENCRYPTION_KEY first; nothing changed.") from None

        with transaction.atomic():
            user = lock_practice_user(initial)
            access = FounderAccess.objects.select_for_update(no_key=connection.vendor == "postgresql").filter(user=user).first()
            if options["action"] == "grant":
                if not founder_role_eligible(user):
                    raise CommandError("Grant requires an active, email-eligible account with the SUPER_ADMIN role and a usable password. No role was changed.")
                if access and access.is_enabled:
                    raise CommandError("This account is already enrolled. Revoke it first only if you intend to replace its authenticator.")
                if not access:
                    access = FounderAccess(user=user)
                access.encrypted_totp_secret = encrypted
                access.is_enabled = True
                access.access_version = new_access_version()
                access.last_accepted_step = -1
                access.failed_attempts = 0
                access.locked_until = None
                access.save()
                founder_audit(None, "FOUNDER_ACCESS_GRANTED", f"Server operator enrolled account #{user.pk}; its existing role was not changed.")
            else:
                if not access or not access.is_enabled:
                    raise CommandError("No enabled founder access exists for that account. Nothing was changed.")
                access.is_enabled = False
                access.encrypted_totp_secret = ""
                access.access_version = new_access_version()
                access.failed_attempts = 0
                access.locked_until = None
                access.save(update_fields=["is_enabled", "encrypted_totp_secret", "access_version",
                                           "failed_attempts", "locked_until", "updated_at"])
                founder_audit(None, "FOUNDER_ACCESS_REVOKED", f"Server operator revoked founder access for account #{user.pk}.")

        if secret:
            self.stdout.write(self.style.WARNING(
                "Authenticator setup secret (shown once). Treat it like a password; do not record this terminal output or share it."))
            self.stdout.write(secret)
            self.stdout.write("Enter it manually into a time-based authenticator (6 digits, 30 seconds). "
                              "Then sign in normally and open /founder/. The account role was not changed.")
        else:
            self.stdout.write(self.style.SUCCESS("Founder access revoked. Existing accounts and roles were not changed."))
