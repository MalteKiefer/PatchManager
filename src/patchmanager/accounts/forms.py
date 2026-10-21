"""Forms for authentication, account settings and administration."""

from typing import Any

from django import forms
from django.utils.translation import gettext_lazy as _

from patchmanager.accounts import permissions
from patchmanager.accounts.models import Assignment, Group, Role, User
from patchmanager.tenancy.models import Tenant

_PASSWORD = {"autocomplete": "current-password"}
_NEW_PASSWORD = {"autocomplete": "new-password"}


class LoginForm(forms.Form):
    """Email and password."""

    email = forms.EmailField(
        label=_("Email"),
        widget=forms.EmailInput(attrs={"autocomplete": "username", "autofocus": True}),
    )
    password = forms.CharField(
        label=_("Password"), strip=False, widget=forms.PasswordInput(attrs=_PASSWORD)
    )


class CodeForm(forms.Form):
    """Six digit TOTP code or a recovery code."""

    code = forms.CharField(
        label=_("Code"),
        max_length=20,
        widget=forms.TextInput(
            attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "autofocus": True}
        ),
    )


class SetupForm(forms.Form):
    """First run: create the initial super admin."""

    token = forms.CharField(
        label=_("Setup token"),
        max_length=64,
        help_text=_("Run the setup_token command to get one."),
    )
    email = forms.EmailField(label=_("Email"))
    display_name = forms.CharField(label=_("Name"), max_length=150)
    password = forms.CharField(
        label=_("Password"), strip=False, widget=forms.PasswordInput(attrs=_NEW_PASSWORD)
    )
    password_confirm = forms.CharField(
        label=_("Repeat password"), strip=False, widget=forms.PasswordInput(attrs=_NEW_PASSWORD)
    )

    def clean(self) -> dict[str, Any]:
        """Both password fields must match."""
        data = super().clean() or {}
        if data.get("password") != data.get("password_confirm"):
            self.add_error("password_confirm", _("The passwords do not match."))
        return data


class SetPasswordForm(forms.Form):
    """Choose a password (invitation)."""

    password = forms.CharField(
        label=_("Password"), strip=False, widget=forms.PasswordInput(attrs=_NEW_PASSWORD)
    )
    password_confirm = forms.CharField(
        label=_("Repeat password"), strip=False, widget=forms.PasswordInput(attrs=_NEW_PASSWORD)
    )

    def clean(self) -> dict[str, Any]:
        """Both password fields must match."""
        data = super().clean() or {}
        if data.get("password") != data.get("password_confirm"):
            self.add_error("password_confirm", _("The passwords do not match."))
        return data


class ChangePasswordForm(SetPasswordForm):
    """Change the own password."""

    current = forms.CharField(
        label=_("Current password"), strip=False, widget=forms.PasswordInput(attrs=_PASSWORD)
    )
    field_order = ["current", "password", "password_confirm"]


class ProfileForm(forms.ModelForm):  # type: ignore[type-arg]
    """Own display name and language."""

    class Meta:
        model = User
        fields = ["display_name", "language"]


class NameForm(forms.Form):
    """Name a new factor."""

    name = forms.CharField(label=_("Name"), max_length=100, initial=_("Authenticator app"))


class InviteForm(forms.Form):
    """Invite a user with an initial role."""

    email = forms.EmailField(label=_("Email"))
    display_name = forms.CharField(label=_("Name"), max_length=150)
    role = forms.ModelChoiceField(label=_("Role"), queryset=Role.objects.all())
    tenant = forms.ModelChoiceField(
        label=_("Tenant"),
        queryset=Tenant.objects.none(),
        required=False,
        empty_label=_("All tenants (global)"),
    )

    def __init__(self, *args: Any, tenants: Any = None, **kwargs: Any) -> None:
        """``tenants``: the tenants visible to the inviting admin."""
        super().__init__(*args, **kwargs)
        self.fields["tenant"].queryset = tenants if tenants is not None else Tenant.objects.all()  # type: ignore[attr-defined]


class AssignmentForm(forms.Form):
    """Grant or deny a role, optionally within a tenant."""

    role = forms.ModelChoiceField(label=_("Role"), queryset=Role.objects.all())
    tenant = forms.ModelChoiceField(
        label=_("Scope"), queryset=Tenant.objects.none(), required=False, empty_label=_("Global")
    )
    effect = forms.ChoiceField(
        label=_("Effect"), choices=Assignment.Effect.choices, initial=Assignment.Effect.ALLOW
    )
    conditions = forms.JSONField(
        label=_("Conditions (JSON)"),
        required=False,
        initial=list,
        help_text=_('Optional, e.g. [{"attr": "status", "op": "ne", "value": "approved"}]'),
    )
    object_type = forms.ChoiceField(label=_("Conditions apply to"), required=False)
    location = forms.ChoiceField(
        label=_("Location"),
        required=False,
        help_text=_("Narrows the scope to a site, building, floor, room or rack."),
    )

    def __init__(
        self, *args: Any, tenants: Any = None, locations: Any = None, **kwargs: Any
    ) -> None:
        """``tenants`` and ``locations``: scopes the acting admin can see."""
        super().__init__(*args, **kwargs)
        self.fields["tenant"].queryset = tenants if tenants is not None else Tenant.objects.all()  # type: ignore[attr-defined]
        self.fields["location"].choices = [("", "-"), *(locations or [])]  # type: ignore[attr-defined]
        self.fields["object_type"].choices = [("", "-")] + [  # type: ignore[attr-defined]
            (ot.code, ot.label) for ot in permissions.object_types() if ot.condition_attrs
        ]


class RoleForm(forms.ModelForm):  # type: ignore[type-arg]
    """Custom role; permissions come from the matrix checkboxes."""

    class Meta:
        model = Role
        fields = ["name", "description", "requires_mfa"]


class CloneForm(forms.Form):
    """Name for a cloned role."""

    name = forms.CharField(label=_("Name of the copy"), max_length=150)


class ImportForm(forms.Form):
    """Role definitions file."""

    file = forms.FileField(label=_("JSON or YAML file"))

    def clean_file(self) -> str:
        """Read at most 1 MB of UTF-8 text."""
        upload = self.cleaned_data["file"]
        if upload.size > 1_000_000:
            raise forms.ValidationError(_("The file is too large."))
        try:
            return str(upload.read().decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise forms.ValidationError(_("The file must be UTF-8 text.")) from exc


class GroupForm(forms.ModelForm):  # type: ignore[type-arg]
    """Group name and description."""

    class Meta:
        model = Group
        fields = ["name", "description"]


class MembersForm(forms.Form):
    """Direct members of a group."""

    members = forms.ModelMultipleChoiceField(
        label=_("Members"),
        queryset=User.objects.all(),
        required=False,
        widget=forms.SelectMultiple(attrs={"size": 10}),
    )


class ParentForm(forms.Form):
    """Nest a group in another group."""

    parent = forms.ModelChoiceField(label=_("Add to group"), queryset=Group.objects.all())


class ReasonForm(forms.Form):
    """Justification for impersonation."""

    reason = forms.CharField(
        label=_("Reason"), max_length=500, widget=forms.Textarea(attrs={"rows": 2})
    )


class ExplainForm(forms.Form):
    """Inputs of the permission explainer."""

    user = forms.ModelChoiceField(label=_("User"), queryset=User.objects.all())
    permission = forms.ChoiceField(label=_("Permission"))
    tenant = forms.ModelChoiceField(
        label=_("On tenant"),
        queryset=Tenant.objects.all(),
        required=False,
        empty_label=_("Global (no object)"),
    )

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Offer every registered permission."""
        super().__init__(*args, **kwargs)
        self.fields["permission"].choices = [(p, p) for p in permissions.all_permissions()]  # type: ignore[attr-defined]
