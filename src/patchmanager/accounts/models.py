"""Identity, sessions, MFA factors and role based access control."""

from typing import Any, ClassVar

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.postgres.fields import ArrayField
from django.contrib.sessions.base_session import AbstractBaseSession
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from patchmanager.core.fields import LtreeField
from patchmanager.core.models import TimestampedModel, UUIDModel


class UserManager(BaseUserManager["User"]):
    """Email based user creation."""

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        """Create a user with a normalised email and hashed password."""
        if not extra.get("is_service"):
            from patchmanager.instance import state

            extra.setdefault("language", state.get().default_language)
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        user.set_password(password)  # None stores an unusable password
        user.save(using=self._db)
        return user

    def get_by_natural_key(self, username: str | None) -> User:
        """Case insensitive lookup by email."""
        return self.get(email__iexact=username)


class User(AbstractBaseUser, TimestampedModel):
    """A person. Users are global; tenant access comes from role assignments."""

    # Set for the duration of an API request by the token authentication (not stored).
    api_token_limits: Any = None

    email = models.EmailField(_("email"), max_length=254)
    display_name = models.CharField(_("display name"), max_length=150)
    language = models.CharField(
        _("language"), max_length=8, choices=settings.LANGUAGES, default="en"
    )
    is_active = models.BooleanField(_("active"), default=True)
    locked_until = models.DateTimeField(_("locked until"), null=True, blank=True)
    failed_login_count = models.PositiveIntegerField(default=0)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    # API client principal: no password, no web sign in
    is_service = models.BooleanField(default=False)
    # May sign in with a password even where SSO is enforced (emergency access)
    break_glass = models.BooleanField(_("break glass"), default=False)
    # SCIM: created by provisioning (only these SCIM may change), the client's id,
    # deactivated by SCIM (only then may SCIM reactivate), deprovisioned by SCIM
    scim_provisioned = models.BooleanField(default=False)
    scim_external_id = models.CharField(max_length=255, blank=True)
    scim_deactivated = models.BooleanField(default=False)
    scim_deleted_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[UserManager] = UserManager()

    USERNAME_FIELD = "email"
    EMAIL_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = ["display_name"]
    policy_object_type = "accounts.user"
    policy_path = None

    class Meta:
        ordering = ["email"]
        constraints = [models.UniqueConstraint(Lower("email"), name="user_email_unique")]

    def __str__(self) -> str:
        return self.email

    @property
    def is_locked(self) -> bool:
        """Locked by too many failed logins or by an administrator."""
        return self.locked_until is not None and self.locked_until > timezone.now()

    # Django admin compatibility. Real authorisation goes through accounts.policy.
    @property
    def is_staff(self) -> bool:
        """Only super admins may use the raw Django admin."""
        from patchmanager.accounts import policy

        return policy.is_super_admin(self)

    is_superuser = is_staff

    def has_perm(self, perm: str, obj: object = None) -> bool:
        """Django admin hook: super admins only."""
        return self.is_staff

    def has_module_perms(self, app_label: str) -> bool:
        """Django admin hook: super admins only."""
        return self.is_staff


class UserSession(AbstractBaseSession):
    """Server side session with owner and metadata, so it can be listed and revoked."""

    user = models.ForeignKey(User, null=True, on_delete=models.CASCADE, related_name="sessions")
    ip_address = models.GenericIPAddressField(null=True)
    user_agent = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(null=True)
    last_activity = models.DateTimeField(null=True)

    @classmethod
    def get_session_store_class(cls) -> Any:
        """Store class used by ``SESSION_ENGINE``."""
        from patchmanager.accounts.sessions import SessionStore

        return SessionStore


class TOTPDevice(TimestampedModel):
    """Time based one time password authenticator (RFC 6238)."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="totp_devices")
    name = models.CharField(max_length=100)
    encrypted_secret = models.TextField()
    confirmed = models.BooleanField(default=False)
    last_used_step = models.BigIntegerField(default=0)


class WebAuthnCredential(TimestampedModel):
    """Security key or passkey registered via WebAuthn."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="webauthn_credentials")
    name = models.CharField(max_length=100)
    credential_id = models.BinaryField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.PositiveBigIntegerField(default=0)
    transports = ArrayField(models.CharField(max_length=20), default=list, blank=True)
    discoverable = models.BooleanField(default=False)
    last_used_at = models.DateTimeField(null=True)


class RecoveryCode(UUIDModel):
    """Single use backup code. Only an HMAC of the code is stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="recovery_codes")
    code_hash = models.CharField(max_length=64)
    used_at = models.DateTimeField(null=True)


class Group(TimestampedModel):
    """Set of users. Groups may contain other groups (no cycles)."""

    name = models.CharField(_("name"), max_length=150)
    description = models.TextField(_("description"), blank=True)
    members = models.ManyToManyField(User, related_name="pm_groups", blank=True)
    parents = models.ManyToManyField("self", symmetrical=False, related_name="children", blank=True)
    external_id = models.CharField(max_length=255, blank=True)  # IdP / SCIM mapping
    scim_managed = models.BooleanField(default=False)  # created by SCIM; only these SCIM may change
    scim_token = (
        models.ForeignKey(  # the SCIM token that created the group, which alone may change it
            "scim.ScimToken",
            null=True,
            blank=True,
            on_delete=models.SET_NULL,
            related_name="groups",
        )
    )

    policy_object_type = "accounts.group"
    policy_path = None

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(Lower("name"), name="group_name_unique")]

    def __str__(self) -> str:
        return self.name


class Role(TimestampedModel):
    """Named bundle of permission codes. System roles are immutable."""

    key = models.SlugField(max_length=64, unique=True)
    name = models.CharField(_("name"), max_length=150)
    description = models.TextField(_("description"), blank=True)
    permissions = ArrayField(models.CharField(max_length=100), default=list, blank=True)
    is_system = models.BooleanField(default=False)
    requires_mfa = models.BooleanField(_("require MFA"), default=False)

    policy_object_type = "accounts.role"
    policy_path = None

    class Meta:
        ordering = ["-is_system", "name"]

    def __str__(self) -> str:
        return str(self.label)

    @property
    def label(self) -> str:
        """Translated name for system roles, stored name for custom roles."""
        from patchmanager.accounts.roles import SYSTEM_ROLES

        system = SYSTEM_ROLES.get(self.key) if self.is_system else None
        return str(system.name) if system else self.name

    @property
    def summary(self) -> str:
        """Translated description for system roles."""
        from patchmanager.accounts.roles import SYSTEM_ROLES

        system = SYSTEM_ROLES.get(self.key) if self.is_system else None
        return str(system.description) if system else self.description


class Assignment(TimestampedModel):
    """Grants (or denies) a role to a user or group within a scope.

    ``scope_path`` is ``NULL`` for global scope, otherwise the ltree path of
    the scope object; it covers that object and everything below it.
    """

    class Effect(models.TextChoices):
        ALLOW = "allow", _("allow")
        DENY = "deny", _("deny")

    role = models.ForeignKey(Role, on_delete=models.PROTECT, related_name="assignments")
    user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.CASCADE, related_name="assignments"
    )
    group = models.ForeignKey(
        Group, null=True, blank=True, on_delete=models.CASCADE, related_name="assignments"
    )
    effect = models.CharField(max_length=5, choices=Effect.choices, default=Effect.ALLOW)
    scope_path = LtreeField(null=True, blank=True)
    scope_label = models.CharField(max_length=300, blank=True)
    tenant = models.ForeignKey(
        "tenancy.Tenant", null=True, blank=True, on_delete=models.CASCADE, related_name="+"
    )
    conditions = models.JSONField(default=list, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")

    policy_object_type = "accounts.assignment"

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(user__isnull=False, group__isnull=True)
                | Q(user__isnull=True, group__isnull=False),
                name="assignment_one_principal",
            ),
        ]
        indexes = [models.Index(fields=["user"]), models.Index(fields=["group"])]

    @property
    def policy_path(self) -> str | None:
        """An assignment is governed by the scope it grants."""
        return str(self.scope_path) if self.scope_path else None


class Invitation(TimestampedModel):
    """One time link that lets an invited user set a password."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="invitation")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    invited_by = models.ForeignKey(User, null=True, on_delete=models.SET_NULL, related_name="+")


class SetupToken(UUIDModel):
    """Short lived token that authorises the first run setup."""

    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
