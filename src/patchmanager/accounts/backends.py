"""Email and password authentication. Lockout and rate limits live in services.auth."""

from typing import Any

from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.hashers import make_password
from django.http import HttpRequest

from patchmanager.accounts.models import User


class EmailBackend(BaseBackend):
    """Authenticate by case insensitive email."""

    def authenticate(
        self,
        request: HttpRequest | None,
        email: str | None = None,
        password: str | None = None,
        **kwargs: Any,
    ) -> User | None:
        """Return the user if the password matches and the account may log in."""
        if not email or not password:
            return None
        user = User.objects.filter(email__iexact=email.strip()).first()
        if user is None:
            make_password(password)  # same cost as a real check: no user enumeration by timing
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None

    def user_can_authenticate(self, user: User) -> bool:
        """Active, not deprovisioned, not locked."""
        return user.is_active and user.deactivated_at is None and not user.is_locked

    def get_user(self, user_id: Any) -> User | None:
        """Session lookup."""
        user = User.objects.filter(pk=user_id).first()
        return user if user is not None and self.user_can_authenticate(user) else None
