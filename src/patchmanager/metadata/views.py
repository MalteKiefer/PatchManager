"""Upload, download and remove attachments; add and remove notes."""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_GET, require_POST

from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.instance import config as instance_config
from patchmanager.metadata import services
from patchmanager.metadata.models import Attachment, Note


class UploadForm(forms.Form):
    """One file plus an optional caption."""

    file = forms.FileField(label=gettext_lazy("File"))
    caption = forms.CharField(label=gettext_lazy("Caption"), max_length=300, required=False)


class NoteForm(forms.Form):
    """Markdown note."""

    body = forms.CharField(
        label=gettext_lazy("Note"),
        max_length=20000,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text=gettext_lazy("Markdown is supported."),
    )


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _back(request: HttpRequest) -> HttpResponse:
    target = request.POST.get("next", "/")
    if url_has_allowed_host_and_scheme(
        target, {request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(target)
    return redirect("/")


@login_required
@require_POST
def upload(request: HttpRequest, label: str, object_id: str) -> HttpResponse:
    """Attach a file to an object."""
    form = UploadForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, _("Choose a file to upload."))
        return _back(request)
    upload_file = form.cleaned_data["file"]
    try:
        target = services.resolve(label, object_id)
        if upload_file.size > instance_config.get().upload_max_bytes:
            raise ServiceError(_("The file is too large."))
        services.attach(
            request,
            _actor(request),
            target,
            name=upload_file.name,
            data=upload_file.read(),
            caption=form.cleaned_data["caption"],
        )
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    except ServiceError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("File attached."))
    return _back(request)


@login_required
@require_POST
def note(request: HttpRequest, label: str, object_id: str) -> HttpResponse:
    """Add a note to an object."""
    form = NoteForm(request.POST)
    try:
        target = services.resolve(label, object_id)
        if not form.is_valid():
            raise ServiceError(_("The note is empty."))
        services.add_note(request, _actor(request), target, form.cleaned_data["body"])
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    except ServiceError as exc:
        messages.error(request, str(exc))
    return _back(request)


@login_required
@require_GET
def download(request: HttpRequest, pk: str) -> HttpResponse:
    """Serve a decrypted attachment with headers that prevent rendering as active content."""
    attachment = get_object_or_404(Attachment, pk=pk)  # RLS hides other tenants
    try:
        _target, data = services.read(request.user, attachment)
    except PermissionDeniedError as exc:
        raise Http404 from exc
    response = HttpResponse(data, content_type=attachment.mime_type)
    inline = attachment.kind == Attachment.Kind.IMAGE and request.GET.get("download") is None
    disposition = "inline" if inline else "attachment"
    response["Content-Disposition"] = f'{disposition}; filename="{attachment.original_name}"'
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = (
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    )
    response["Cache-Control"] = "private, max-age=3600"
    return response


@login_required
@require_POST
def remove(request: HttpRequest, pk: str) -> HttpResponse:
    """Delete an attachment."""
    attachment = get_object_or_404(Attachment, pk=pk)
    try:
        services.remove(request, _actor(request), attachment)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    except ServiceError as exc:
        messages.error(request, str(exc))
    return _back(request)


@login_required
@require_POST
def remove_note(request: HttpRequest, pk: str) -> HttpResponse:
    """Delete a note."""
    item = get_object_or_404(Note, pk=pk)
    try:
        services.remove_note(request, _actor(request), item)
    except PermissionDeniedError as exc:
        raise PermissionDenied from exc
    return _back(request)
