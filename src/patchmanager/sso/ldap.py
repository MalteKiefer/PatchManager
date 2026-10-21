"""LDAP sign in with python-ldap: bind as the service account, find the person, bind as them.

Filters are escaped (no LDAP injection), empty passwords are refused (an
unauthenticated bind would succeed), certificates are verified, referrals are
not followed and timeouts are short.
"""

from typing import Any

import ldap as pyldap
from django.utils.translation import gettext as _
from ldap.filter import escape_filter_chars

from patchmanager.accounts.services import ServiceError
from patchmanager.core import crypto
from patchmanager.sso.models import IdentityProvider
from patchmanager.sso.services import Assertion

TIMEOUT = 5


def _connect(provider: IdentityProvider) -> Any:
    cfg = provider.config
    uri = str(cfg.get("server_uri", ""))
    connection = pyldap.initialize(uri)
    connection.set_option(pyldap.OPT_REFERRALS, 0)
    connection.set_option(pyldap.OPT_NETWORK_TIMEOUT, TIMEOUT)
    connection.set_option(pyldap.OPT_TIMEOUT, TIMEOUT)
    connection.set_option(pyldap.OPT_X_TLS_REQUIRE_CERT, pyldap.OPT_X_TLS_DEMAND)
    if cfg.get("ca_file"):
        connection.set_option(pyldap.OPT_X_TLS_CACERTFILE, cfg["ca_file"])
    connection.set_option(pyldap.OPT_X_TLS_NEWCTX, 0)  # apply the TLS options above
    if uri.startswith("ldap://") and cfg.get("start_tls", True):
        connection.start_tls_s()
    return connection


def _text(values: list[bytes] | None) -> list[str]:
    return [v.decode("utf-8", "replace") for v in values or []]


def authenticate(provider: IdentityProvider, email: str, password: str) -> Assertion | None:
    """The person if the password is right, else None. Raises ServiceError if LDAP is unusable."""
    if not password:
        return None
    cfg = provider.config
    attrs = {
        "id": cfg.get("id_attribute") or "entryUUID",
        "email": cfg.get("email_attribute") or "mail",
        "name": cfg.get("name_attribute") or "displayName",
        "groups": cfg.get("groups_attribute") or "memberOf",
    }
    query = str(cfg.get("user_filter") or "(mail={email})").replace(
        "{email}", escape_filter_chars(email.strip())
    )
    secret = crypto.decrypt(provider.encrypted_secret) if provider.encrypted_secret else ""
    try:
        service = _connect(provider)
        try:
            service.simple_bind_s(cfg.get("bind_dn", ""), secret)
        except pyldap.INVALID_CREDENTIALS as exc:
            raise ServiceError(_("The directory refused the service account.")) from exc
        found = service.search_ext_s(
            cfg.get("user_base", ""), pyldap.SCOPE_SUBTREE, query, list(attrs.values()), sizelimit=2
        )
        service.unbind_s()
    except pyldap.SIZELIMIT_EXCEEDED:
        return None  # ambiguous
    except pyldap.LDAPError as exc:
        raise ServiceError(_("The directory is not reachable.")) from exc
    entries = [(dn, values) for dn, values in found if dn]  # skip referrals
    if len(entries) != 1:
        return None
    dn, values = entries[0]
    try:
        person = _connect(provider)
        person.simple_bind_s(dn, password)
        person.unbind_s()
    except pyldap.INVALID_CREDENTIALS:
        return None
    except pyldap.LDAPError as exc:
        raise ServiceError(_("The directory is not reachable.")) from exc

    def first(name: str) -> str:
        found_values = _text(values.get(name))
        return found_values[0] if found_values else ""

    return Assertion(
        subject=first(attrs["id"]) or dn,
        email=first(attrs["email"]) or email,
        name=first(attrs["name"]),
        groups=_text(values.get(attrs["groups"])),
        email_verified=True,  # the directory is authoritative for its domains
    )
