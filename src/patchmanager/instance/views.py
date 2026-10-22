"""Branding and settings pages, and the public branding assets."""

import ipaddress
from dataclasses import asdict
from typing import Any

from django import forms
from django.conf import settings
from django.contrib import messages
from django.core.validators import RegexValidator
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET

from patchmanager.accounts.decorators import permission_required
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
from patchmanager.instance import config as instance_config
from patchmanager.instance import services, state


class BrandingForm(forms.Form):
    """How the instance presents itself."""

    name = forms.CharField(
        label=gettext_lazy("Instance name"),
        max_length=100,
        required=False,
        help_text=gettext_lazy("Empty: PATCHMANAGER_INSTANCE_NAME."),
    )
    brand_hue = forms.IntegerField(
        label=gettext_lazy("Brand colour (hue, 0 to 359)"), min_value=0, max_value=359
    )
    default_language = forms.ChoiceField(
        label=gettext_lazy("Default language for new accounts"), choices=settings.LANGUAGES
    )
    logo = forms.FileField(label=gettext_lazy("Logo (PNG or JPEG, up to 512 KB)"), required=False)
    remove_logo = forms.BooleanField(label=gettext_lazy("Remove the logo"), required=False)


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: permission_required logs in
    return request.user


@permission_required("system.settings.change")
def branding(request: HttpRequest) -> HttpResponse:
    """Name, colour, logo and default language."""
    snapshot = state.get()
    form = BrandingForm(
        request.POST or None,
        request.FILES or None,
        initial={
            "name": snapshot.name,
            "brand_hue": snapshot.brand_hue,
            "default_language": snapshot.default_language,
        },
    )
    if request.method == "POST" and form.is_valid():
        upload = form.cleaned_data["logo"]
        try:
            services.save_branding(
                request,
                _actor(request),
                name=form.cleaned_data["name"],
                brand_hue=form.cleaned_data["brand_hue"],
                default_language=form.cleaned_data["default_language"],
                logo=upload.read(services.LOGO_MAX_BYTES + 1) if upload else None,
                remove_logo=form.cleaned_data["remove_logo"],
            )
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Branding saved."))
            return redirect("instance:branding")
    return render(request, "instance/branding.html", {"form": form, "has_logo": snapshot.has_logo})


@require_GET
def branding_css(request: HttpRequest) -> HttpResponse:
    """The brand colour scale (public: the sign in page uses it too)."""
    response = HttpResponse(
        services.brand_css(state.get().brand_hue), content_type="text/css; charset=utf-8"
    )
    response["Cache-Control"] = "public, max-age=86400"  # the URL carries the settings version
    return response


@require_GET
def logo(request: HttpRequest) -> HttpResponse:
    """The uploaded logo (public, re-encoded PNG)."""
    data = services.logo_bytes()
    if data is None:
        raise Http404
    response = HttpResponse(data, content_type="image/png")
    response["Cache-Control"] = "public, max-age=86400"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


# --- Operational settings -------------------------------------------------------------

SECRET_HELP = gettext_lazy("Stored encrypted. Leave empty to keep the current value.")


def _secret(label: Any) -> forms.CharField:
    return forms.CharField(
        label=label,
        required=False,
        max_length=500,
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
        help_text=SECRET_HELP,
    )


class SecurityForm(forms.Form):
    """Sign in and sessions."""

    password_min_length = forms.IntegerField(
        label=gettext_lazy("Minimum password length"), min_value=8, max_value=128
    )
    mfa_required = forms.BooleanField(
        label=gettext_lazy("Require a second factor for everyone"), required=False
    )
    session_max_age_hours = forms.IntegerField(
        label=gettext_lazy("Session lifetime (hours)"), min_value=1, max_value=720
    )
    session_idle_minutes = forms.IntegerField(
        label=gettext_lazy("Sign out after inactivity (minutes)"), min_value=5, max_value=1440
    )
    login_max_failures = forms.IntegerField(
        label=gettext_lazy("Lock an account after failed sign ins"), min_value=3, max_value=100
    )
    login_lockout_minutes = forms.IntegerField(
        label=gettext_lazy("Lock duration (minutes)"), min_value=1, max_value=1440
    )
    login_rate_per_ip = forms.IntegerField(
        label=gettext_lazy("Sign in attempts per address in 10 minutes"),
        min_value=5,
        max_value=1000,
    )
    invitation_ttl_hours = forms.IntegerField(
        label=gettext_lazy("Invitation links are valid for (hours)"), min_value=1, max_value=720
    )


class MailForm(forms.Form):
    """The SMTP server for invitations, resets and notifications."""

    mail_host = forms.CharField(
        label=gettext_lazy("SMTP server"),
        required=False,
        max_length=253,
        validators=[RegexValidator(r"^[A-Za-z0-9.-]+$", gettext_lazy("Enter a host name."))],
        help_text=gettext_lazy("Empty: mails are not sent (shown in the log in development)."),
    )
    mail_port = forms.IntegerField(label=gettext_lazy("Port"), min_value=1, max_value=65535)
    mail_use_tls = forms.BooleanField(label=gettext_lazy("Use STARTTLS"), required=False)
    mail_user = forms.CharField(label=gettext_lazy("User"), required=False, max_length=254)
    mail_password = _secret(gettext_lazy("Password"))
    mail_password_clear = forms.BooleanField(
        label=gettext_lazy("Remove the stored password"), required=False
    )
    mail_from = forms.EmailField(label=gettext_lazy("Sender address"))


class StorageForm(forms.Form):
    """Upload limits, virus scanning and where files are kept."""

    upload_max_mb = forms.IntegerField(
        label=gettext_lazy("Largest upload (MB)"),
        min_value=1,
        max_value=settings.UPLOAD_LIMIT_MB,
    )
    clamd_host = forms.CharField(
        label=gettext_lazy("Virus scanner (clamd host)"),
        required=False,
        max_length=253,
        validators=[RegexValidator(r"^[A-Za-z0-9.-]+$", gettext_lazy("Enter a host name."))],
        help_text=gettext_lazy("Empty: no scan. When set, uploads fail while it is unreachable."),
    )
    clamd_port = forms.IntegerField(label=gettext_lazy("clamd port"), min_value=1, max_value=65535)
    s3_bucket = forms.CharField(
        label=gettext_lazy("S3 bucket"),
        required=False,
        max_length=63,
        validators=[
            RegexValidator(
                r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", gettext_lazy("Enter a valid bucket name.")
            )
        ],
        help_text=gettext_lazy(
            "Empty: files stay in the media volume. Files already stored there stay readable."
        ),
    )
    s3_endpoint_url = forms.URLField(
        label=gettext_lazy("Endpoint"),
        required=False,
        assume_scheme="https",
        help_text=gettext_lazy(
            "Empty for AWS. Backblaze B2 for example https://s3.eu-central-003.backblazeb2.com."
        ),
    )
    s3_region = forms.CharField(label=gettext_lazy("Region"), required=False, max_length=50)
    s3_access_key_id = forms.CharField(
        label=gettext_lazy("Access key ID"), required=False, max_length=200
    )
    s3_secret_access_key = _secret(gettext_lazy("Secret access key"))
    s3_prefix = forms.CharField(
        label=gettext_lazy("Folder in the bucket"),
        max_length=100,
        validators=[
            RegexValidator(r"^[A-Za-z0-9._/-]+$", gettext_lazy("Letters, digits, . _ - /"))
        ],
    )
    s3_addressing_style = forms.ChoiceField(
        label=gettext_lazy("Addressing"),
        required=False,
        choices=[
            ("", gettext_lazy("Provider default")),
            ("virtual", gettext_lazy("Virtual host (bucket.host)")),
            ("path", gettext_lazy("Path (host/bucket)")),
        ],
    )

    def clean_s3_endpoint_url(self) -> str:
        """Only https endpoints."""
        url = str(self.cleaned_data.get("s3_endpoint_url") or "")
        if url and not url.startswith("https://"):
            raise forms.ValidationError(_("The endpoint must use https."))
        return url


class MapsForm(forms.Form):
    """Background tiles of the site map."""

    map_tile_url = forms.CharField(
        label=gettext_lazy("Tile server"),
        required=False,
        max_length=300,
        help_text=gettext_lazy("https with {z}, {x} and {y}. Empty: no map."),
    )
    map_attribution = forms.CharField(
        label=gettext_lazy("Attribution"), required=False, max_length=200
    )

    def clean_map_tile_url(self) -> str:
        """An https tile template."""
        url = str(self.cleaned_data.get("map_tile_url") or "").strip()
        if url and (
            not url.startswith("https://") or not all(p in url for p in ("{z}", "{x}", "{y}"))
        ):
            raise forms.ValidationError(_("Enter an https address with {z}, {x} and {y}."))
        return url


class IntegrationsForm(forms.Form):
    """Webhooks and the API."""

    webhook_allowed_networks = forms.CharField(
        label=gettext_lazy("Private networks webhooks may reach"),
        required=False,
        max_length=1000,
        help_text=gettext_lazy("Comma separated, for example 10.0.0.0/8. Empty: public only."),
    )
    webhook_allow_http = forms.BooleanField(
        label=gettext_lazy("Allow plain http webhook URLs"), required=False
    )
    api_rate = forms.RegexField(
        label=gettext_lazy("API requests per user"),
        regex=r"^[1-9][0-9]{0,6}/(second|minute|hour|day)$",
        error_messages={"invalid": gettext_lazy("For example 1200/minute.")},
    )

    def clean_webhook_allowed_networks(self) -> str:
        """Normalise the networks."""
        nets = [n.strip() for n in self.cleaned_data["webhook_allowed_networks"].split(",")]
        try:
            return ", ".join(str(ipaddress.ip_network(n, strict=False)) for n in nets if n)
        except ValueError as exc:
            raise forms.ValidationError(_("Enter networks such as 10.0.0.0/8.")) from exc


SECTIONS: dict[str, tuple[Any, type[forms.Form], Any]] = {
    "security": (
        gettext_lazy("Sign in"),
        SecurityForm,
        gettext_lazy("Passwords, second factors, sessions and lockouts."),
    ),
    "mail": (
        gettext_lazy("Mail"),
        MailForm,
        gettext_lazy("The server for invitations, password resets and notifications."),
    ),
    "storage": (
        gettext_lazy("Files"),
        StorageForm,
        gettext_lazy(
            "Uploads are encrypted before they are stored, in the media volume or in S3"
            " compatible object storage (AWS S3, Backblaze B2, Wasabi). The bucket is checked"
            " with a test file when you save."
        ),
    ),
    "maps": (gettext_lazy("Maps"), MapsForm, gettext_lazy("Tiles shown behind the site map.")),
    "integrations": (
        gettext_lazy("Integrations"),
        IntegrationsForm,
        gettext_lazy("Limits for webhooks and the API."),
    ),
}


@permission_required("system.settings.change")
def settings_view(request: HttpRequest, section: str = "security") -> HttpResponse:
    """One section of the operational settings."""
    if section not in SECTIONS:
        raise Http404
    title, form_class, intro = SECTIONS[section]
    current = asdict(instance_config.get())
    form = form_class(
        request.POST or None,
        initial={k: v for k, v in current.items() if k not in instance_config.SECRETS},
    )
    if request.method == "POST" and form.is_valid():
        values = {k: v for k, v in form.cleaned_data.items() if k in instance_config.NAMES}
        if form.cleaned_data.get("mail_password_clear"):
            values["mail_password"] = None
        try:
            services.save_config(request, _actor(request), values)
        except ServiceError as exc:
            form.add_error(None, str(exc))
        else:
            messages.success(request, _("Settings saved."))
            return redirect("instance:settings", section)
    secret = {"mail": "mail_password", "storage": "s3_secret_access_key"}.get(section)
    return render(
        request,
        "instance/settings.html",
        {
            "form": form,
            "section": section,
            "title": title,
            "intro": intro,
            "sections": [(key, label) for key, (label, _f, _i) in SECTIONS.items()],
            "secret_stored": bool(secret and current.get(secret)),
        },
    )
