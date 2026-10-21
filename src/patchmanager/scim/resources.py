"""SCIM Users and Groups (RFC 7643) on top of accounts.User and accounts.Group.

Safeguards: SCIM never changes super admins, break glass accounts or API client
service users, and only manages groups it created itself (``scim_managed``), so
a provisioning client cannot hand out roles it was not meant to.
"""

from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.http import HttpRequest
from django.utils import timezone

from patchmanager.accounts import policy
from patchmanager.accounts.models import Group, User
from patchmanager.accounts.services.users import _revoke
from patchmanager.audit import services as audit
from patchmanager.scim.filters import Attribute

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

USER_FILTERS = {
    "id": Attribute("pk", "uuid"),
    "username": Attribute("email"),
    "externalid": Attribute("scim_external_id"),
    "displayname": Attribute("display_name"),
    "name.formatted": Attribute("display_name"),
    "emails.value": Attribute("email"),
    "emails": Attribute("email"),
    "active": Attribute("is_active", "bool"),
    "meta.created": Attribute("created_at", "datetime"),
    "meta.lastmodified": Attribute("updated_at", "datetime"),
}
GROUP_FILTERS = {
    "id": Attribute("pk", "uuid"),
    "displayname": Attribute("name"),
    "externalid": Attribute("external_id"),
    "members.value": Attribute("members__pk", "uuid"),
    "members": Attribute("members__pk", "uuid"),
    "meta.created": Attribute("created_at", "datetime"),
    "meta.lastmodified": Attribute("updated_at", "datetime"),
}


class ScimError(Exception):
    """An error in SCIM terms (RFC 7644 section 3.12)."""

    def __init__(self, status: int, detail: str, scim_type: str = "") -> None:
        super().__init__(detail)
        self.status, self.detail, self.scim_type = status, detail, scim_type


def version(obj: Any) -> str:
    """Weak ETag from the last modification time."""
    return f'W/"{int(obj.updated_at.timestamp() * 1_000_000)}"'


def check_version(request: HttpRequest, obj: Any) -> None:
    """Honour If-Match: refuse changes based on a stale copy."""
    wanted = request.headers.get("If-Match")
    if (
        wanted
        and wanted.strip() != "*"
        and version(obj) not in [v.strip() for v in wanted.split(",")]
    ):
        raise ScimError(412, "The resource was changed since it was read.")


def _meta(request: HttpRequest, obj: Any, kind: str) -> dict[str, Any]:
    return {
        "resourceType": kind,
        "created": obj.created_at.isoformat(),
        "lastModified": obj.updated_at.isoformat(),
        "location": request.build_absolute_uri(f"/scim/v2/{kind}s/{obj.pk}"),
        "version": version(obj),
    }


# ---------------------------------------------------------------- users


def users() -> QuerySet[User]:
    """Users SCIM sees: everyone except API clients and SCIM deprovisioned accounts."""
    return User.objects.filter(is_service=False, scim_deleted_at__isnull=True).prefetch_related(
        "pm_groups"
    )


def protected(user: User) -> bool:
    """Accounts SCIM must not change."""
    return user.is_service or user.break_glass or policy.is_super_admin(user)


def user_json(request: HttpRequest, user: User) -> dict[str, Any]:
    """RFC 7643 User."""
    return {
        "schemas": [USER_SCHEMA],
        "id": str(user.pk),
        "externalId": user.scim_external_id or None,
        "userName": user.email,
        "name": {"formatted": user.display_name},
        "displayName": user.display_name,
        "emails": [{"value": user.email, "type": "work", "primary": True}],
        "active": user.is_active and user.deactivated_at is None,
        "groups": [
            {"value": str(g.pk), "display": g.name, "type": "direct"}
            for g in user.pm_groups.all()
            if g.scim_managed
        ],
        "meta": _meta(request, user, "User"),
    }


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"  # Microsoft Entra sends strings
    raise ScimError(400, "active must be true or false.", "invalidValue")


def _user_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Model fields from a (partial) SCIM User; keys are matched case insensitively."""
    lowered = {str(k).lower(): v for k, v in data.items()}
    fields: dict[str, Any] = {}
    email = lowered.get("username")
    emails = lowered.get("emails")
    if not email and isinstance(emails, list) and emails:
        primary = next((e for e in emails if isinstance(e, dict) and e.get("primary")), emails[0])
        email = primary.get("value") if isinstance(primary, dict) else None
    if email is not None:
        email = str(email).strip().lower()
        try:
            validate_email(email)
        except ValidationError as exc:
            raise ScimError(400, "userName must be an email address.", "invalidValue") from exc
        fields["email"] = email
    raw_name = lowered.get("name")
    name: dict[str, Any] = raw_name if isinstance(raw_name, dict) else {}
    display = (
        lowered.get("displayname")
        or name.get("formatted")
        or " ".join(str(p) for p in (name.get("givenName"), name.get("familyName")) if p)
    )
    if display:
        fields["display_name"] = str(display)[:150]
    if "externalid" in lowered:
        fields["scim_external_id"] = str(lowered["externalid"] or "")[:255]
    if "active" in lowered:
        fields["is_active"] = _bool(lowered["active"])
    return fields


def _apply_user(request: HttpRequest, user: User, fields: dict[str, Any], token: str) -> User:
    if protected(user):
        raise ScimError(403, "This account cannot be changed by provisioning.")
    active = fields.pop("is_active", None)
    for key, value in fields.items():
        setattr(user, key, value)
    if active is not None:
        user.is_active = active
        user.deactivated_at = None if active else (user.deactivated_at or timezone.now())
    try:
        with transaction.atomic():
            user.save()
    except IntegrityError as exc:
        raise ScimError(409, "userName is already in use.", "uniqueness") from exc
    if active is False:
        _revoke(user)
    audit.record(
        "scim.user.updated",
        obj=user,
        data={
            "token": token,
            "fields": sorted([*fields, *(["active"] if active is not None else [])]),
        },
    )
    return user


def create_user(request: HttpRequest, data: dict[str, Any], token: str) -> User:
    """POST /Users. A user SCIM deprovisioned earlier comes back instead of a conflict."""
    fields = _user_fields(data)
    if "email" not in fields:
        raise ScimError(400, "userName is required.", "invalidValue")
    existing = User.objects.filter(email__iexact=fields["email"]).first()
    if existing is not None:
        if existing.scim_deleted_at is None or existing.is_service:
            raise ScimError(409, "userName is already in use.", "uniqueness")
        existing.scim_deleted_at = None
        fields.setdefault("is_active", True)
        return _apply_user(request, existing, fields, token)
    active = fields.pop("is_active", True)
    user = User.objects.create_user(
        password=None, **{"display_name": fields.pop("display_name", fields["email"]), **fields}
    )
    if not active:
        User.objects.filter(pk=user.pk).update(is_active=False, deactivated_at=timezone.now())
        user.refresh_from_db()
    audit.record("scim.user.created", obj=user, data={"token": token})
    return user


def replace_user(request: HttpRequest, user: User, data: dict[str, Any], token: str) -> User:
    """PUT /Users/<id>."""
    fields = _user_fields(data)
    if "email" not in fields:
        raise ScimError(400, "userName is required.", "invalidValue")
    fields.setdefault("is_active", True)
    return _apply_user(request, user, fields, token)


def patch_user(
    request: HttpRequest, user: User, operations: list[dict[str, Any]], token: str
) -> User:
    """PATCH /Users/<id> with add, replace and remove operations."""
    fields: dict[str, Any] = {}
    for operation in operations:
        op = str(operation.get("op", "")).lower()
        path = str(operation.get("path") or "")
        value = operation.get("value")
        if op not in ("add", "replace", "remove"):
            raise ScimError(400, "Unknown operation.", "invalidSyntax")
        if op == "remove":
            if path.lower() == "externalid":
                fields["scim_external_id"] = ""
                continue
            raise ScimError(400, f"{path or 'This attribute'} cannot be removed.", "mutability")
        if not path:
            if not isinstance(value, dict):
                raise ScimError(
                    400, "An operation without path needs an object value.", "invalidValue"
                )
            fields |= _user_fields(value)
            continue
        key = path.lower()
        if key in ("name.formatted", "displayname", "name.givenname", "name.familyname"):
            fields["display_name"] = str(value)[:150]
        elif key in ('emails[type eq "work"].value', "emails"):
            fields |= _user_fields(
                {
                    "emails": value
                    if isinstance(value, list)
                    else [{"value": value, "primary": True}]
                }
            )
        else:
            fields |= _user_fields({path: value})
    return _apply_user(request, user, fields, token)


def delete_user(request: HttpRequest, user: User, token: str) -> None:
    """DELETE /Users/<id>: deprovision (deactivate and hide) instead of erasing the history."""
    if protected(user):
        raise ScimError(403, "This account cannot be changed by provisioning.")
    with transaction.atomic():
        User.objects.filter(pk=user.pk).update(
            is_active=False,
            deactivated_at=timezone.now(),
            scim_deleted_at=timezone.now(),
            updated_at=timezone.now(),
        )
        user.pm_groups.remove(*Group.objects.filter(scim_managed=True))
        _revoke(user)
    policy.bump_version()
    audit.record("scim.user.deleted", obj=user, data={"token": token})


# ---------------------------------------------------------------- groups


def groups() -> QuerySet[Group]:
    """Groups SCIM may see and change: those it created."""
    return Group.objects.filter(scim_managed=True).prefetch_related("members")


def group_json(request: HttpRequest, group: Group) -> dict[str, Any]:
    """RFC 7643 Group."""
    return {
        "schemas": [GROUP_SCHEMA],
        "id": str(group.pk),
        "externalId": group.external_id or None,
        "displayName": group.name,
        "members": [
            {"value": str(u.pk), "display": u.email, "type": "User"} for u in group.members.all()
        ],
        "meta": _meta(request, group, "Group"),
    }


def _members(values: Any) -> list[User]:
    if not isinstance(values, list):
        raise ScimError(400, "members must be a list.", "invalidValue")
    ids = []
    for member in values:
        if not isinstance(member, dict) or "value" not in member:
            raise ScimError(400, "Each member needs a value.", "invalidValue")
        if str(member.get("type", "User")).lower() == "group":
            raise ScimError(400, "Nested groups are not supported.", "invalidValue")
        ids.append(str(member["value"]))
    try:
        found = list(users().filter(pk__in=ids))
    except ValidationError as exc:
        raise ScimError(400, "Unknown member.", "invalidValue") from exc
    if len(found) != len(set(ids)):
        raise ScimError(400, "Unknown member.", "invalidValue")
    if any(protected(u) for u in found):
        raise ScimError(403, "Protected accounts cannot be provisioned.")
    return found


def _save_group(
    group: Group,
    token: str,
    action: str,
    members: list[User] | None = None,
    add: list[User] | None = None,
    remove: list[User] | None = None,
) -> Group:
    try:
        with transaction.atomic():
            group.save()
            if members is not None:
                kept = [
                    u for u in group.members.all() if protected(u)
                ]  # never drop protected members
                group.members.set([*members, *kept])
            if add:
                group.members.add(*add)
            if remove:
                group.members.remove(*[u for u in remove if not protected(u)])
    except IntegrityError as exc:
        raise ScimError(409, "displayName is already in use.", "uniqueness") from exc
    policy.bump_version()
    audit.record(f"scim.group.{action}", data={"token": token, "group": group.name})
    return group


def create_group(request: HttpRequest, data: dict[str, Any], token: str) -> Group:
    """POST /Groups."""
    name = str(data.get("displayName") or "").strip()
    if not name:
        raise ScimError(400, "displayName is required.", "invalidValue")
    members = _members(data.get("members", []))
    group = Group(
        name=name[:150], external_id=str(data.get("externalId") or "")[:255], scim_managed=True
    )
    return _save_group(group, token, "created", members=members)


def replace_group(request: HttpRequest, group: Group, data: dict[str, Any], token: str) -> Group:
    """PUT /Groups/<id>."""
    name = str(data.get("displayName") or "").strip()
    if not name:
        raise ScimError(400, "displayName is required.", "invalidValue")
    group.name, group.external_id = name[:150], str(data.get("externalId") or "")[:255]
    return _save_group(group, token, "updated", members=_members(data.get("members", [])))


def patch_group(
    request: HttpRequest, group: Group, operations: list[dict[str, Any]], token: str
) -> Group:
    """PATCH /Groups/<id>: rename, add or remove members (as Entra and Okta send them)."""
    add: list[User] = []
    remove: list[User] = []
    members: list[User] | None = None
    for operation in operations:
        op = str(operation.get("op", "")).lower()
        path = str(operation.get("path") or "")
        value = operation.get("value")
        lowered = path.lower()
        if op not in ("add", "replace", "remove"):
            raise ScimError(400, "Unknown operation.", "invalidSyntax")
        if lowered == "displayname" or (
            not path and isinstance(value, dict) and "displayName" in value
        ):
            name = value if isinstance(value, str) else (value or {}).get("displayName", "")
            if not str(name).strip():
                raise ScimError(400, "displayName is required.", "invalidValue")
            group.name = str(name).strip()[:150]
        elif lowered == "externalid":
            group.external_id = "" if op == "remove" else str(value or "")[:255]
        elif lowered == "members" and op == "add":
            add += _members(value)
        elif lowered == "members" and op == "replace":
            members = _members(value)
        elif lowered == "members" and op == "remove":
            remove += _members(value) if value else list(group.members.all())
        elif lowered.startswith("members[value eq ") and op == "remove":
            member_id = lowered.removeprefix("members[value eq ").strip('"]')
            remove += _members([{"value": member_id}])
        else:
            raise ScimError(400, f"{path or 'This operation'} is not supported.", "invalidPath")
    return _save_group(group, token, "updated", members=members, add=add, remove=remove)


def delete_group(request: HttpRequest, group: Group, token: str) -> None:
    """DELETE /Groups/<id> (only groups SCIM created)."""
    name = group.name
    group.delete()
    policy.bump_version()
    audit.record("scim.group.deleted", data={"token": token, "group": name})
