"""Edition and branding pages, and the public branding assets."""

from django import forms
from django.conf import settings
from django.contrib import messages
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET

from patchmanager.accounts.decorators import permission_required
from patchmanager.accounts.models import User
from patchmanager.accounts.services import ServiceError
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
