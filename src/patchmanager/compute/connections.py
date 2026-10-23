"""Proxmox VE connections: what to take over, synchronisation by hand and on a schedule."""

import logging
from datetime import timedelta
from typing import Any

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods

from patchmanager.accounts import policy
from patchmanager.accounts.models import User
from patchmanager.accounts.services import PermissionDeniedError, ServiceError
from patchmanager.audit import services as audit
from patchmanager.compute import importing
from patchmanager.compute import inventory as sources
from patchmanager.compute.models import Connection, VirtualMachine
from patchmanager.core import crypto
from patchmanager.core.fields import path_label
from patchmanager.dcim.models import Device
from patchmanager.library.models import DeviceType
from patchmanager.locations.models import Room

logger = logging.getLogger(__name__)


def fetch(connection: Connection) -> sources.Inventory:
    """What the platform reports now (the stored token is decrypted only for this)."""
    return sources.proxmox(
        connection.url, crypto.decrypt(connection.secret), connection.fingerprint
    )


def sync(
    connection: Connection, actor: User, request: HttpRequest | None = None
) -> importing.Result:
    """Take over the chosen nodes, guests and storage; new guests too if wanted."""
    data = fetch(connection)
    chosen = set(connection.guests)
    if connection.add_new_guests:
        known = set(connection.guests) | set(connection.seen)
        new = {g.external_id for g in data.guests} - known
        chosen |= new
        connection.guests = sorted(chosen)
    pick = importing.Pick(
        nodes=set(connection.nodes),
        guests=chosen,
        storage=set(connection.storage),
        room=connection.node_room,
        device_type=connection.node_type,
    )
    if connection.cluster is None:
        _refuse_cluster_of_another_connection(connection, data)
    result = importing.save(
        request, actor, connection.tenant, data, pick=pick, cluster=connection.cluster
    )
    gone = (
        VirtualMachine.objects.filter(cluster=result.cluster, external_id__in=chosen)
        .exclude(external_id__in=[g.external_id for g in data.guests])
        .values_list("name", flat=True)
    )
    notes = [
        *result.notes,
        *(_("%(name)s is no longer on the platform.") % {"name": n} for n in gone),
    ]
    connection.cluster = result.cluster
    connection.last_synced_at = timezone.now()
    connection.last_error = ""
    connection.last_result = _(
        "%(servers)s new servers, %(created)s new and %(updated)s updated machines,"
        " %(storage)s storage."
    ) % {
        "servers": result.servers,
        "created": result.created,
        "updated": result.updated,
        "storage": result.storage,
    }
    if notes:
        connection.last_result += "\n" + "\n".join(notes[:50])
    connection.save(
        update_fields=[
            "cluster",
            "guests",
            "last_synced_at",
            "last_error",
            "last_result",
            "updated_at",
        ]
    )
    return result


def _refuse_cluster_of_another_connection(connection: Connection, data: sources.Inventory) -> None:
    """A new connection must not take over a cluster another connection updates.

    Clusters are found by name, and machines inside a cluster by their platform ID, so
    two platforms with the same cluster (or node) name would overwrite each other.
    """
    name = data.cluster or data.platform
    other = (
        Connection.objects.filter(tenant=connection.tenant, cluster__name__iexact=name)
        .exclude(pk=connection.pk)
        .first()
    )
    if other is not None:
        raise ServiceError(
            _(
                "The cluster %(cluster)s is already updated by the connection %(other)s."
                " Rename the documented cluster first."
            )
            % {"cluster": name, "other": other.name}
        )


def due() -> list[Any]:
    """Claim the connections whose interval has passed (marked as started, so none runs twice)."""
    from patchmanager.tenancy import rls

    with rls.system_context():
        now = timezone.now()
        claimed = []
        for connection in Connection.objects.filter(interval__gt=0).only(
            "pk", "interval", "last_synced_at"
        ):
            last = connection.last_synced_at
            if last is None or last + timedelta(hours=connection.interval) <= now:
                # Claim with a compare and set: a second scheduler run skips it.
                taken = Connection.objects.filter(pk=connection.pk, last_synced_at=last).update(
                    last_synced_at=now
                )
                if taken:
                    claimed.append(connection.pk)
        return claimed


def sync_one(pk: Any) -> bool:
    """Update one connection; any failure is recorded on it and never stops the others."""
    from patchmanager.tenancy import rls

    with rls.system_context():
        connection = (
            Connection.objects.filter(pk=pk)
            .select_related("tenant", "created_by", "cluster", "node_room", "node_type")
            .first()
        )
        if connection is None:
            return False
        actor = connection.created_by
        try:
            if actor is None or not actor.is_active:
                raise ServiceError(
                    _("The person who set up this connection can no longer sign in.")
                )
            sync(connection, actor)
        except (ServiceError, PermissionDeniedError, ValidationError) as exc:
            message = str(getattr(exc, "messages", [exc])[0])
        except Exception as exc:  # recorded, so one broken platform stops nothing
            logger.exception("Update of connection %s failed", pk)
            message = _("The update failed (%(kind)s).") % {"kind": type(exc).__name__}
        else:
            return True
        Connection.objects.filter(pk=pk).update(last_error=message[:300])
        return False


def sync_due() -> int:
    """Update every due connection, one after the other (used without a task queue)."""
    return sum(sync_one(pk) for pk in due())


# --- Pages -------------------------------------------------------------------------------


class ConnectionForm(forms.ModelForm):  # type: ignore[type-arg]
    """Address and token of a Proxmox VE cluster."""

    token = forms.CharField(
        label=gettext_lazy("API token"),
        widget=forms.PasswordInput(attrs={"autocomplete": "off"}),
        help_text=gettext_lazy(
            "user@realm!tokenid=secret of a token with read rights (role PVEAuditor). It is"
            " stored encrypted and never shown again."
        ),
    )

    class Meta:
        model = Connection
        fields = ["name", "url", "fingerprint"]
        help_texts = {
            "url": gettext_lazy("For example https://pve1.example.lan:8006."),
            "fingerprint": gettext_lazy("Only for self signed certificates."),
        }


class SelectionForm(forms.Form):
    """What to take over, how to create servers, and how often to update."""

    nodes = forms.MultipleChoiceField(
        label=gettext_lazy("Nodes"), required=False, widget=forms.CheckboxSelectMultiple
    )
    guests = forms.MultipleChoiceField(
        label=gettext_lazy("Virtual machines and containers"),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    storage = forms.MultipleChoiceField(
        label=gettext_lazy("Storage"), required=False, widget=forms.CheckboxSelectMultiple
    )
    add_new_guests = forms.BooleanField(
        label=gettext_lazy("Take over new machines automatically"), required=False
    )
    node_room = forms.ModelChoiceField(
        label=gettext_lazy("Room for new servers"), queryset=Room.objects.none(), required=False
    )
    node_type = forms.ModelChoiceField(
        label=gettext_lazy("Device type for new servers"),
        queryset=DeviceType.objects.none(),
        required=False,
    )
    interval = forms.TypedChoiceField(
        label=gettext_lazy("Automatic update"), coerce=int, choices=Connection.Interval.choices
    )

    def __init__(
        self,
        *args: Any,
        data_found: sources.Inventory,
        connection: Connection,
        actor: Any,
        **kwargs: Any,
    ) -> None:
        """Choices from what the platform reports, marked when already documented."""
        super().__init__(*args, **kwargs)
        tenant = connection.tenant
        devices = {
            d.name.lower() for d in Device.objects.filter(tenant=tenant, name__in=data_found.hosts)
        }
        documented = (
            set(
                VirtualMachine.objects.filter(tenant=tenant, cluster=connection.cluster)
                .exclude(external_id="")
                .values_list("external_id", flat=True)
            )
            if connection.cluster_id
            else set()
        )
        on = _(" (documented)")
        fields: Any = self.fields
        fields["nodes"].choices = [
            (n, n + (on if n.lower() in devices else _(" (new server)"))) for n in data_found.hosts
        ]
        fields["guests"].choices = [
            (
                g.external_id,
                f"{g.external_id} · {g.name} · {g.host} · {g.status}"
                + (on if g.external_id in documented else ""),
            )
            for g in sorted(data_found.guests, key=lambda g: (g.host, g.name))
        ]
        fields["storage"].choices = [(s.name, s.name) for s in data_found.storage]
        rooms: Any = self.fields["node_room"]
        rooms.queryset = policy.scope_queryset(
            actor, "dcim.device.add", Room.objects.filter(tenant=tenant)
        )
        types: Any = self.fields["node_type"]
        from django.db.models import Q

        types.queryset = DeviceType.objects.filter(
            Q(tenant__isnull=True) | Q(tenant=tenant)
        ).select_related("vendor")

    def clean(self) -> dict[str, Any]:
        """New servers need both a room and a device type."""
        data = super().clean() or {}
        if bool(data.get("node_room")) != bool(data.get("node_type")):
            raise forms.ValidationError(
                _("Choose both a room and a device type for new servers, or neither.")
            )
        return data


def _actor(request: HttpRequest) -> User:
    assert isinstance(request.user, User)  # noqa: S101  # nosec B101: views require login
    return request.user


def _connection(request: HttpRequest, pk: Any, action: str) -> Connection:
    connection = get_object_or_404(Connection.objects.select_related("tenant", "cluster"), pk=pk)
    if not policy.has_perm(request.user, "compute.cluster.view", connection):
        raise Http404
    if not policy.has_perm(request.user, f"compute.cluster.{action}", connection):
        raise PermissionDenied
    return connection


@login_required
def connection_list(request: HttpRequest) -> HttpResponse:
    """Connections of the current tenant."""
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    rows = [
        c
        for c in Connection.objects.filter(tenant=tenant).select_related("cluster")
        if policy.has_perm(request.user, "compute.cluster.view", c)
    ]
    return render(
        request,
        "compute/connections.html",
        {
            "connections": rows,
            "can_add": policy.has_perm(
                request.user, "compute.cluster.add", scope_path=path_label(tenant.pk)
            ),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def connection_create(request: HttpRequest) -> HttpResponse:
    """Page that connects a Proxmox VE cluster (the token is checked before it is kept)."""
    tenant = getattr(request, "tenant", None)
    if tenant is None:
        raise Http404
    actor = _actor(request)
    if not policy.has_perm(actor, "compute.cluster.add", scope_path=path_label(tenant.pk)):
        raise PermissionDenied
    form = ConnectionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        token = form.cleaned_data["token"]
        try:
            found = sources.proxmox(
                form.cleaned_data["url"], token, form.cleaned_data["fingerprint"]
            )
            connection = form.save(commit=False)
            connection.tenant, connection.created_by = tenant, actor
            connection.secret = crypto.encrypt(token)
            connection.nodes = list(found.hosts)
            connection.storage = [s.name for s in found.storage]
            connection.guests = [g.external_id for g in found.guests]
            connection.seen = list(connection.guests)
            connection.full_clean(exclude=["tenant", "secret"])
            connection.save()
        except ServiceError as exc:
            form.add_error(None, str(exc))
        except ValidationError as exc:
            form.add_error(None, "; ".join(exc.messages))
        else:
            audit.record(
                "compute.connection.created",
                request=request,
                obj=connection,
                data={"name": connection.name},
            )
            messages.success(request, _("Connected. Choose what to take over."))
            return redirect("compute:connection-select", connection.pk)
    return render(
        request,
        "core/create.html",
        {
            "title": _("Connect Proxmox VE"),
            "intro": _(
                "PatchManager reads nodes, virtual machines, containers and storage of the"
                " cluster through its API. It never changes anything there."
            ),
            "form": form,
            "back_url": "/compute/connections/",
            "submit": _("Connect"),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def connection_select(request: HttpRequest, pk: str) -> HttpResponse:
    """Choose nodes, guests and storage to take over; saving runs the first update."""
    connection = _connection(request, pk, "change")
    actor = _actor(request)
    try:
        found = fetch(connection)
    except ServiceError as exc:
        messages.error(request, str(exc))
        return redirect("compute:connection", connection.pk)
    initial = {
        "nodes": connection.nodes,
        "guests": connection.guests,
        "storage": connection.storage,
        "add_new_guests": connection.add_new_guests,
        "node_room": connection.node_room_id,
        "node_type": connection.node_type_id,
        "interval": connection.interval,
    }
    form = SelectionForm(
        request.POST or None, initial=initial, data_found=found, connection=connection, actor=actor
    )
    if (
        request.method == "POST"
        and form.is_valid()
        and form.cleaned_data["add_new_guests"]
        and not policy.has_perm(
            actor, "compute.vm.add", scope_path=path_label(connection.tenant_id)
        )
    ):
        form.add_error("add_new_guests", _("You may not add virtual machines."))
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        connection.nodes, connection.guests, connection.storage = (
            data["nodes"],
            data["guests"],
            data["storage"],
        )
        connection.add_new_guests, connection.interval = data["add_new_guests"], data["interval"]
        connection.node_room, connection.node_type = data["node_room"], data["node_type"]
        # Scheduled updates act as whoever last chose what is taken over, so nobody can
        # schedule work that runs with someone else's rights.
        connection.created_by = actor
        # Guests offered now but left out stay out, also with "take over new machines".
        connection.seen = [g.external_id for g in found.guests]
        connection.last_result = ""
        connection.save()
        audit.record(
            "compute.connection.changed",
            request=request,
            obj=connection,
            data={
                "guests": len(connection.guests),
                "add_new_guests": connection.add_new_guests,
                "interval": connection.interval,
            },
        )
        try:
            result = sync(connection, actor, request)
        except PermissionDeniedError as exc:
            raise PermissionDenied from exc
        except (ServiceError, ValidationError) as exc:
            messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        else:
            messages.success(
                request, _("Updated: %(n)s machines.") % {"n": result.created + result.updated}
            )
        return redirect("compute:connection", connection.pk)
    return render(
        request,
        "compute/connection_select.html",
        {
            "connection": connection,
            "form": form,
            "found": found,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def connection_detail(request: HttpRequest, pk: str) -> HttpResponse:
    """A connection: last update, update now, change the selection, remove it."""
    connection = _connection(request, pk, "view")
    actor = _actor(request)
    can_change = policy.has_perm(actor, "compute.cluster.change", connection)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "sync" and can_change:
            try:
                sync(connection, actor, request)
                messages.success(request, _("Updated."))
            except PermissionDeniedError as exc:
                raise PermissionDenied from exc
            except (ServiceError, ValidationError) as exc:
                Connection.objects.filter(pk=connection.pk).update(last_error=str(exc)[:300])
                messages.error(request, "; ".join(getattr(exc, "messages", [str(exc)])))
        elif action == "delete" and policy.has_perm(actor, "compute.cluster.delete", connection):
            audit.record(
                "compute.connection.deleted",
                request=request,
                obj=connection,
                data={"name": connection.name},
            )
            connection.delete()
            messages.success(request, _("Connection removed; the documented machines stay."))
            return redirect("compute:connections")
        else:
            raise PermissionDenied
        return redirect("compute:connection", connection.pk)
    return render(
        request,
        "compute/connection.html",
        {
            "connection": connection,
            "can_change": can_change,
            "can_delete": policy.has_perm(actor, "compute.cluster.delete", connection),
        },
    )
