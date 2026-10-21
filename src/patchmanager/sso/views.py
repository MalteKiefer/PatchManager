"""Sign in endpoints of the identity providers, and their administration."""

from typing import Any

from django import forms
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from patchmanager.accounts.decorators import permission_required
from patchmanager.accounts.models import Group
from patchmanager.accounts.services import ServiceError
from patchmanager.audit import services as audit
from patchmanager.core import crypto, ratelimit
from patchmanager.sso import oidc, saml, services
from patchmanager.sso.models import IdentityProvider

ATTEMPTS_PER_IP = 30  # sign in starts and callbacks per 10 minutes


def _provider(slug: str, kind: str | None = None) -> IdentityProvider:
    provider = IdentityProvider.objects.filter(slug=slug, enabled=True).first()
    if provider is None or (kind and provider.kind != kind):
        raise Http404
    return provider


def _limited(request: HttpRequest) -> bool:
    return not ratelimit.hit(
        f"sso:{request.META.get('REMOTE_ADDR')}", limit=ATTEMPTS_PER_IP, window=600
    )


def _done(
    request: HttpRequest, provider: IdentityProvider, assertion: services.Assertion
) -> HttpResponse:
    try:
        outcome = services.sign_in(request, provider, assertion)
    except ServiceError as exc:
        audit.record(
            "sso.login.failed",
            request=request,
            data={"provider": provider.slug, "reason": str(exc)},
        )
        messages.error(request, str(exc))
        return redirect("accounts:login")
    return redirect("accounts:mfa") if outcome == "mfa" else redirect("core:home")


def _failed(request: HttpRequest, provider: IdentityProvider, exc: ServiceError) -> HttpResponse:
    audit.record(
        "sso.login.failed", request=request, data={"provider": provider.slug, "reason": str(exc)}
    )
    messages.error(request, str(exc))
    return redirect("accounts:login")


@require_GET
def login(request: HttpRequest, slug: str) -> HttpResponse:
    """Send the browser to the identity provider."""
    provider = _provider(slug)
    if _limited(request):
        messages.error(request, _("Too many attempts. Try again in a few minutes."))
        return redirect("accounts:login")
    try:
        if provider.kind == IdentityProvider.Kind.OIDC:
            return HttpResponseRedirect(
                oidc.start(request, provider, request.GET.get("login_hint", ""))
            )
        if provider.kind == IdentityProvider.Kind.SAML:
            return saml.start(request, provider)
    except ServiceError as exc:
        return _failed(request, provider, exc)
    raise Http404  # LDAP signs in through the password form


@require_GET
def oidc_callback(request: HttpRequest, slug: str) -> HttpResponse:
    """The provider sends the browser back with a code."""
    provider = _provider(slug, IdentityProvider.Kind.OIDC)
    if _limited(request):
        return redirect("accounts:login")
    try:
        assertion = oidc.finish(request, provider)
    except ServiceError as exc:
        return _failed(request, provider, exc)
    return _done(request, provider, assertion)


@csrf_exempt  # the IdP posts cross site; the signed response and InResponseTo protect the flow
@require_POST
def saml_acs(request: HttpRequest, slug: str) -> HttpResponse:
    """Assertion consumer service (HTTP-POST binding)."""
    provider = _provider(slug, IdentityProvider.Kind.SAML)
    if _limited(request):
        return redirect("accounts:login")
    try:
        assertion = saml.finish(request, provider)
    except ServiceError as exc:
        return _failed(request, provider, exc)
    return _done(request, provider, assertion)


@require_GET
def saml_metadata(request: HttpRequest, slug: str) -> HttpResponse:
    """Service provider metadata to import at the IdP."""
    provider = _provider(slug, IdentityProvider.Kind.SAML)
    try:
        return HttpResponse(
            saml.metadata(request, provider), content_type="application/samlmetadata+xml"
        )
    except ServiceError as exc:
        raise Http404 from exc


@require_GET
def saml_sls(request: HttpRequest, slug: str) -> HttpResponse:
    """The IdP confirms a single logout: the local session already ended."""
    _provider(slug, IdentityProvider.Kind.SAML)
    messages.info(request, _("You have been signed out."))
    return redirect("accounts:login")


def single_logout_url(request: HttpRequest) -> str | None:
    """Where to send the browser after the local logout, to end the IdP session too."""
    provider_id = request.session.get(services.SSO_PROVIDER)
    data = request.session.get(services.SSO_LOGOUT) or {}
    provider = (
        IdentityProvider.objects.filter(pk=provider_id, enabled=True).first()
        if provider_id
        else None
    )
    if provider is None or not provider.config.get("single_logout", True):
        return None
    try:
        if data.get("kind") == "oidc":
            return oidc.logout_url(request, provider, data)
        if data.get("kind") == "saml":
            return saml.logout_url(request, provider, data)
    except ServiceError:
        return None
    return None


# ---------------------------------------------------------------- administration

CONFIG_FIELDS = {
    IdentityProvider.Kind.OIDC: [
        "issuer",
        "client_id",
        "scopes",
        "email_claim",
        "name_claim",
        "groups_claim",
        "trust_email",
    ],
    IdentityProvider.Kind.SAML: [
        "idp_metadata",
        "nameid_format",
        "email_attribute",
        "name_attribute",
        "groups_attribute",
    ],
    IdentityProvider.Kind.LDAP: [
        "server_uri",
        "start_tls",
        "ca_file",
        "bind_dn",
        "user_base",
        "user_filter",
        "id_attribute",
        "email_attribute",
        "name_attribute",
        "groups_attribute",
    ],
}
BOOLEAN = {"trust_email", "start_tls", "single_logout"}


class ProviderForm(forms.Form):
    """All settings; only those of the chosen type are used."""

    name = forms.CharField(label=gettext_lazy("Name"), max_length=100)
    slug = forms.SlugField(label=gettext_lazy("Identifier"), max_length=50)
    kind = forms.ChoiceField(label=gettext_lazy("Type"), choices=IdentityProvider.Kind.choices)
    enabled = forms.BooleanField(label=gettext_lazy("Enabled"), required=False)
    domains = forms.CharField(
        label=gettext_lazy("Email domains"),
        required=False,
        help_text=gettext_lazy(
            "Comma separated. The provider may link existing accounts of these domains."
        ),
    )
    enforce = forms.BooleanField(
        label=gettext_lazy("Enforce for these domains (except break glass accounts)"),
        required=False,
    )
    jit = forms.BooleanField(
        label=gettext_lazy("Create accounts on first sign in"), required=False, initial=True
    )
    mfa_trusted = forms.BooleanField(
        label=gettext_lazy("The provider enforces MFA"), required=False
    )
    single_logout = forms.BooleanField(
        label=gettext_lazy("Single logout"), required=False, initial=True
    )
    secret = forms.CharField(
        label=gettext_lazy("Client secret or bind password"),
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy("Leave empty to keep the stored one."),
    )
    group_mappings = forms.CharField(
        label=gettext_lazy("Group mapping"),
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=gettext_lazy("One per line: external group = group name"),
    )
    issuer = forms.URLField(label="OIDC issuer", required=False, assume_scheme="https")
    client_id = forms.CharField(label="OIDC client_id", required=False)
    scopes = forms.CharField(label="OIDC scopes", required=False, initial="openid email profile")
    email_claim = forms.CharField(label="OIDC email claim", required=False, initial="email")
    name_claim = forms.CharField(label="OIDC name claim", required=False, initial="name")
    groups_claim = forms.CharField(label="OIDC groups claim", required=False, initial="groups")
    trust_email = forms.BooleanField(
        label=gettext_lazy("Trust unverified email addresses"), required=False
    )
    idp_metadata = forms.CharField(
        label="SAML IdP metadata (XML)", required=False, widget=forms.Textarea(attrs={"rows": 4})
    )
    nameid_format = forms.CharField(label="SAML NameID format", required=False)
    email_attribute = forms.CharField(label=gettext_lazy("Email attribute"), required=False)
    name_attribute = forms.CharField(label=gettext_lazy("Name attribute"), required=False)
    groups_attribute = forms.CharField(label=gettext_lazy("Groups attribute"), required=False)
    server_uri = forms.CharField(
        label="LDAP URI", required=False, help_text="ldaps://ldap.example.com"
    )
    start_tls = forms.BooleanField(label="LDAP StartTLS", required=False, initial=True)
    ca_file = forms.CharField(label=gettext_lazy("CA file (path in the container)"), required=False)
    bind_dn = forms.CharField(label="LDAP bind DN", required=False)
    user_base = forms.CharField(label="LDAP user base", required=False)
    user_filter = forms.CharField(
        label="LDAP user filter", required=False, initial="(mail={email})"
    )
    id_attribute = forms.CharField(label="LDAP id attribute", required=False, initial="entryUUID")

    def clean_domains(self) -> list[str]:
        """Lower case, unique, plausible."""
        values = sorted(
            {d.strip().lower() for d in self.cleaned_data["domains"].split(",") if d.strip()}
        )
        if any("@" in d or "." not in d for d in values):
            raise forms.ValidationError(_("Enter domains like example.com."))
        return values

    def clean_group_mappings(self) -> dict[str, str]:
        """``external = group name`` lines to ``{external: group id}``."""
        result: dict[str, str] = {}
        for line in self.cleaned_data["group_mappings"].splitlines():
            if not line.strip():
                continue
            external, _sep, name = line.partition("=")
            group = Group.objects.filter(name__iexact=name.strip()).first()
            if not external.strip() or group is None:
                raise forms.ValidationError(
                    _("Unknown group in: %(line)s") % {"line": line.strip()}
                )
            result[external.strip()] = str(group.pk)
        return result

    def clean(self) -> dict[str, Any]:
        """Type specific checks."""
        data = super().clean() or {}
        kind = data.get("kind")
        if data.get("server_uri") and not str(data["server_uri"]).startswith(
            ("ldaps://", "ldap://")
        ):
            self.add_error("server_uri", _("Use ldaps:// or ldap:// with StartTLS."))
        if (
            kind == IdentityProvider.Kind.LDAP
            and str(data.get("server_uri", "")).startswith("ldap://")
            and not data.get("start_tls")
        ):
            self.add_error("start_tls", _("Plain LDAP needs StartTLS."))
        if (
            kind == IdentityProvider.Kind.LDAP
            and data.get("user_filter")
            and "{email}" not in data["user_filter"]
        ):
            self.add_error("user_filter", _("The filter must contain {email}."))
        return data


def _initial(provider: IdentityProvider) -> dict[str, Any]:
    names = {
        g.pk: g.name for g in Group.objects.filter(pk__in=list(provider.group_mappings.values()))
    }
    mapping = "\n".join(f"{k} = {names.get(v, v)}" for k, v in provider.group_mappings.items())
    return {
        "name": provider.name,
        "slug": provider.slug,
        "kind": provider.kind,
        "enabled": provider.enabled,
        "domains": ", ".join(provider.domains),
        "enforce": provider.enforce,
        "jit": provider.jit,
        "mfa_trusted": provider.mfa_trusted,
        "group_mappings": mapping,
        "single_logout": provider.config.get("single_logout", True),
        **provider.config,
    }


def _save(request: HttpRequest, provider: IdentityProvider, form: ProviderForm) -> None:
    data = form.cleaned_data
    provider.name, provider.slug, provider.kind = data["name"], data["slug"], data["kind"]
    provider.enabled, provider.domains, provider.enforce = (
        data["enabled"],
        data["domains"],
        data["enforce"],
    )
    provider.jit, provider.mfa_trusted, provider.group_mappings = (
        data["jit"],
        data["mfa_trusted"],
        data["group_mappings"],
    )
    keys = [*CONFIG_FIELDS[IdentityProvider.Kind(provider.kind)], "single_logout"]
    provider.config = {k: data[k] for k in keys if data.get(k) not in (None, "") or k in BOOLEAN}
    if data["secret"]:
        provider.encrypted_secret = crypto.encrypt(data["secret"])
    if provider.kind == IdentityProvider.Kind.SAML and provider.enabled:
        saml.client(request, provider)  # refuse broken metadata now, not at the first sign in
    provider.save()
    audit.record(
        "sso.provider.saved",
        request=request,
        data={
            "provider": provider.slug,
            "kind": provider.kind,
            "enabled": provider.enabled,
            "enforce": provider.enforce,
        },
    )


@permission_required("system.sso.manage")
def provider_list(request: HttpRequest) -> HttpResponse:
    """Identity providers."""
    return render(request, "sso/providers.html", {"providers": IdentityProvider.objects.all()})


@permission_required("system.sso.manage")
@require_http_methods(["GET", "POST"])
def provider_edit(request: HttpRequest, pk: str | None = None) -> HttpResponse:
    """Create or change a provider."""
    provider = get_object_or_404(IdentityProvider, pk=pk) if pk else IdentityProvider()
    form = ProviderForm(request.POST or None, initial=_initial(provider) if pk else None)
    if request.method == "POST" and form.is_valid():
        if (
            IdentityProvider.objects.filter(slug=form.cleaned_data["slug"])
            .exclude(pk=provider.pk)
            .exists()
        ):
            form.add_error("slug", _("This identifier is taken."))
        else:
            try:
                _save(request, provider, form)
            except ServiceError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, _("Identity provider saved."))
                return redirect("sso:providers")
    urls = {}
    if provider.pk:
        urls = {
            "oidc": request.build_absolute_uri(f"/sso/{provider.slug}/oidc/callback/"),
            "acs": request.build_absolute_uri(f"/sso/{provider.slug}/saml/acs/"),
            "metadata": request.build_absolute_uri(f"/sso/{provider.slug}/saml/metadata/"),
        }
    return render(request, "sso/provider.html", {"form": form, "provider": provider, "urls": urls})


@permission_required("system.sso.manage")
@require_POST
def provider_delete(request: HttpRequest, pk: str) -> HttpResponse:
    """Remove a provider; linked users keep their accounts."""
    provider = get_object_or_404(IdentityProvider, pk=pk)
    audit.record("sso.provider.deleted", request=request, data={"provider": provider.slug})
    provider.delete()
    messages.success(request, _("Identity provider deleted."))
    return redirect("sso:providers")
