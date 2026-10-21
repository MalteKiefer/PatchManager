import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from django.test import Client
from joserfc import jwt
from joserfc.jwk import KeySet, RSAKey

from patchmanager.accounts.models import Group, User
from patchmanager.audit.models import AuditEvent
from patchmanager.core import crypto
from patchmanager.sso import ldap, oidc
from patchmanager.sso.models import ExternalIdentity, IdentityProvider

pytestmark = pytest.mark.django_db

ISSUER = "https://idp.example.com"


class FakeIdP:
    """Discovery, keys and token endpoint of an OpenID provider, in memory."""

    def __init__(self) -> None:
        self.key = RSAKey.generate_key(2048, parameters={"kid": "k1"})
        self.claims: dict[str, Any] = {}
        self.posted: dict[str, Any] = {}

    def get(self, url: str, **kwargs: Any) -> Reply:
        if url.endswith("/.well-known/openid-configuration"):
            return Reply(
                {
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                    "end_session_endpoint": f"{ISSUER}/logout",
                }
            )
        if url.endswith("/jwks"):
            return Reply(KeySet([self.key]).as_dict(private=False))
        raise AssertionError(url)

    def post(self, url: str, data: dict[str, Any], **kwargs: Any) -> Reply:
        self.posted = {"url": url, "data": data, "auth": kwargs.get("auth")}
        header = {"alg": "RS256", "kid": "k1"}
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": "pm",
            "sub": "u-1",
            "iat": now,
            "exp": now + 300,
            "email": "ada@example.com",
            "email_verified": True,
            "name": "Ada",
            **self.claims,
        }
        return Reply({"id_token": jwt.encode(header, claims, self.key), "access_token": "at"})


class Reply:
    def __init__(self, data: Any) -> None:
        self.data = data

    def raise_for_status(self) -> None:
        pass

    def json(self) -> Any:
        return self.data


@pytest.fixture
def idp(monkeypatch, super_admin):
    from django.core.cache import cache

    cache.clear()
    fake = FakeIdP()
    monkeypatch.setattr(oidc.requests, "get", fake.get)
    monkeypatch.setattr(oidc.requests, "post", fake.post)
    provider = IdentityProvider.objects.create(
        name="Corp",
        slug="corp",
        kind="oidc",
        enabled=True,
        domains=["example.com"],
        config={"issuer": ISSUER, "client_id": "pm"},
        encrypted_secret=crypto.encrypt("s3cret"),
    )
    fake.provider = provider
    return fake


def _flow(client: Client, idp: FakeIdP, **claims: Any):
    start = client.get("/sso/corp/login/?login_hint=ada@example.com", secure=True)
    assert start.status_code == 302
    query = parse_qs(urlsplit(start["Location"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["login_hint"] == ["ada@example.com"]
    idp.claims = {"nonce": query["nonce"][0], **claims}
    return client.get(
        f"/sso/corp/oidc/callback/?code=abc&state={query['state'][0]}", secure=True
    ), query


def test_oidc_sign_in_creates_and_links(idp, client: Client) -> None:
    group = Group.objects.create(name="Network team")
    idp.provider.group_mappings = {"net": str(group.pk)}
    idp.provider.save()
    response, _query = _flow(client, idp, groups=["net", "other"])
    assert response.status_code == 302 and response["Location"] == "/"
    user = User.objects.get(email="ada@example.com")
    assert user.display_name == "Ada" and not user.has_usable_password()
    assert ExternalIdentity.objects.get(user=user).subject == "u-1"
    assert list(user.pm_groups.all()) == [group]
    assert idp.posted["data"]["code_verifier"] and idp.posted["auth"] == ("pm", "s3cret")
    assert client.get("/", secure=True).status_code == 200
    logout = client.post("/auth/logout/", secure=True)
    assert logout["Location"].startswith(f"{ISSUER}/logout?id_token_hint=")
    # second sign in: groups follow the provider
    response, _q = _flow(Client(), idp, groups=[])
    assert response.status_code == 302 and not user.pm_groups.exists()


@pytest.mark.parametrize(
    "claims",
    [
        {"nonce": "wrong"},
        {"aud": "other"},
        {"iss": "https://evil.example.com"},
        {"exp": int(time.time()) - 600},
        {"aud": ["pm", "other"]},  # several audiences need azp
    ],
)
def test_oidc_rejects_bad_tokens(idp, client: Client, claims) -> None:
    start = client.get("/sso/corp/login/", secure=True)
    query = parse_qs(urlsplit(start["Location"]).query)
    idp.claims = {"nonce": query["nonce"][0], **claims}
    response = client.get(
        f"/sso/corp/oidc/callback/?code=abc&state={query['state'][0]}", secure=True, follow=True
    )
    assert b"could not be verified" in response.content
    assert not User.objects.filter(email="ada@example.com").exists()


def test_oidc_state_and_linking_rules(idp, client: Client, make_user) -> None:
    client.get("/sso/corp/login/", secure=True)
    bad_state = client.get(
        "/sso/corp/oidc/callback/?code=abc&state=forged", secure=True, follow=True
    )
    assert b"expired" in bad_state.content
    _flow(Client(), idp, email_verified=False)
    assert not User.objects.filter(email="ada@example.com").exists()
    # an existing account of a domain the provider is not responsible for is never taken over
    make_user("eve@other.org")
    other = Client()
    refused, _q = _flow(other, idp, email="eve@other.org", sub="u-2")
    assert refused["Location"] == "/auth/login/"
    assert not ExternalIdentity.objects.filter(subject="u-2").exists()
    idp.provider.jit = False
    idp.provider.save()
    _flow(Client(), idp, email="new@example.com", sub="u-3")
    assert not User.objects.filter(email="new@example.com").exists()
    assert AuditEvent.objects.filter(action="sso.login.failed").count() >= 3


def test_oidc_mfa_handover(idp, client: Client, make_user) -> None:
    from tests.conftest import add_totp

    user = make_user("ada@example.com")
    add_totp(user)
    response, _q = _flow(client, idp)
    assert response["Location"] == "/auth/mfa/"
    idp.provider.mfa_trusted = True
    idp.provider.save()
    response, _q = _flow(Client(), idp)
    assert response["Location"] == "/"


def test_enforced_domain_and_break_glass(idp, client: Client, make_user) -> None:
    idp.provider.enforce = True
    idp.provider.save()
    make_user("ada@example.com")
    response = client.post(
        "/auth/login/", {"email": "ada@example.com", "password": "whatever"}, secure=True
    )
    assert response.status_code == 302 and response["Location"].startswith(
        "/sso/corp/login/?login_hint="
    )
    admin = make_user("root2@example.com", break_glass=True)
    response = client.post("/auth/login/", {"email": admin.email, "password": "wrong"}, secure=True)
    assert response.status_code == 200 and b"incorrect" in response.content
    assert b"Sign in with Corp" in client.get("/auth/login/", secure=True).content


def test_disabled_and_unknown_providers(idp, client: Client) -> None:
    idp.provider.enabled = False
    idp.provider.save()
    assert client.get("/sso/corp/login/", secure=True).status_code == 404
    assert client.get("/sso/nope/login/", secure=True).status_code == 404


# ---------------------------------------------------------------- LDAP


class FakeDirectory:
    """Just enough of python-ldap's connection for the sign in."""

    people = {
        "cn=svc,dc=corp": ("svcpw", {}),
        "uid=bob,ou=people,dc=corp": (
            "bobpw",
            {
                "mail": [b"bob@corp.example"],
                "displayName": [b"Bob"],
                "entryUUID": [b"e-1"],
                "memberOf": [b"cn=net,dc=corp"],
            },
        ),
    }
    searches: list[str] = []

    def set_option(self, *args: Any) -> None:
        pass

    def start_tls_s(self) -> None:
        pass

    def simple_bind_s(self, dn: str, password: str) -> None:
        import ldap as pyldap

        if dn not in self.people or self.people[dn][0] != password:
            raise pyldap.INVALID_CREDENTIALS({"desc": "Invalid credentials"})

    def search_ext_s(self, base, scope, query, attrs, sizelimit=0):
        self.searches.append(query)
        return [
            (dn, values)
            for dn, (_pw, values) in self.people.items()
            if values and f"(mail={values['mail'][0].decode()})" == query
        ] + [(None, ["ldap://ref"])]

    def unbind_s(self) -> None:
        pass


@pytest.fixture
def directory(monkeypatch):
    import ldap as pyldap

    monkeypatch.setattr(pyldap, "initialize", lambda uri: FakeDirectory())
    FakeDirectory.searches = []
    return IdentityProvider.objects.create(
        name="Directory",
        slug="dir",
        kind="ldap",
        enabled=True,
        domains=["corp.example"],
        config={
            "server_uri": "ldap://ldap.corp",
            "start_tls": True,
            "bind_dn": "cn=svc,dc=corp",
            "user_base": "ou=people,dc=corp",
            "user_filter": "(mail={email})",
        },
        encrypted_secret=crypto.encrypt("svcpw"),
    )


def test_ldap_sign_in(directory, client: Client, super_admin) -> None:
    group = Group.objects.create(name="Net")
    directory.group_mappings = {"cn=net,dc=corp": str(group.pk)}
    directory.save()
    bad = client.post(
        "/auth/login/", {"email": "bob@corp.example", "password": "nope"}, secure=True
    )
    assert b"incorrect" in bad.content
    empty = ldap.authenticate(directory, "bob@corp.example", "")
    assert empty is None
    injected = ldap.authenticate(directory, "*)(mail=*", "bobpw")
    assert injected is None and FakeDirectory.searches[-1] == r"(mail=\2a\29\28mail=\2a)"
    ok = client.post(
        "/auth/login/", {"email": "bob@corp.example", "password": "bobpw"}, secure=True
    )
    assert ok.status_code == 302
    user = User.objects.get(email="bob@corp.example")
    assert ExternalIdentity.objects.get(user=user).subject == "e-1" and list(
        user.pm_groups.all()
    ) == [group]
    session_event = AuditEvent.objects.filter(action="auth.login.success").latest("seq")
    assert session_event.data["method"] == "ldap:dir"


def test_ldap_service_account_failure(directory, client: Client, super_admin) -> None:
    directory.encrypted_secret = crypto.encrypt("wrong")
    directory.save()
    response = client.post(
        "/auth/login/", {"email": "bob@corp.example", "password": "bobpw"}, secure=True
    )
    assert b"service account" in response.content


# ---------------------------------------------------------------- administration


def test_provider_admin(admin_client: Client, make_user) -> None:
    Group.objects.create(name="Admins")
    data = {
        "name": "Corp",
        "slug": "corp",
        "kind": "oidc",
        "enabled": "on",
        "domains": "Example.com, corp.example",
        "jit": "on",
        "secret": "s3cret",
        "group_mappings": "admins = Admins",
        "issuer": ISSUER,
        "client_id": "pm",
        "scopes": "openid email",
        "single_logout": "on",
    }
    created = admin_client.post("/manage/sso/new/", data, secure=True)
    assert created.status_code == 302, created.content
    provider = IdentityProvider.objects.get(slug="corp")
    assert (
        provider.domains == ["corp.example", "example.com"]
        and crypto.decrypt(provider.encrypted_secret) == "s3cret"
    )
    assert list(provider.group_mappings) == ["admins"] and provider.config["client_id"] == "pm"
    page = admin_client.get(f"/manage/sso/{provider.pk}/", secure=True).content
    assert b"/sso/corp/oidc/callback/" in page and b"s3cret" not in page
    admin_client.post(f"/manage/sso/{provider.pk}/", {**data, "secret": ""}, secure=True)
    provider.refresh_from_db()
    assert crypto.decrypt(provider.encrypted_secret) == "s3cret"  # kept
    for bad, message in (
        ({"group_mappings": "x = Nobody"}, b"Unknown group"),
        ({"domains": "me@example.com"}, b"domains"),
        ({"kind": "ldap", "server_uri": "ldap://x", "start_tls": ""}, b"StartTLS"),
        ({"kind": "ldap", "server_uri": "ldaps://x", "user_filter": "(uid=x)"}, b"{email}"),
        ({"slug": "corp"}, b"taken"),
    ):
        response = admin_client.post("/manage/sso/new/", {**data, **bad}, secure=True)
        assert message in response.content, bad
    admin_client.post(f"/manage/sso/{provider.pk}/delete/", secure=True)
    assert not IdentityProvider.objects.exists()
    other = Client()
    from tests.conftest import login

    login(other, make_user())
    assert other.get("/manage/sso/", secure=True).status_code == 403


# ---------------------------------------------------------------- SAML


def _key_and_cert(tmp_path):
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "saml-idp.example.com")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = tmp_path / "idp.key", tmp_path / "idp.crt"
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return str(key_path), str(cert_path)


@pytest.fixture
def saml_idp(tmp_path, client: Client, super_admin):
    from saml2 import BINDING_HTTP_REDIRECT
    from saml2.config import IdPConfig
    from saml2.metadata import create_metadata_string
    from saml2.saml import NAME_FORMAT_BASIC, NAMEID_FORMAT_PERSISTENT
    from saml2.server import Server

    from patchmanager.sso import saml

    key, cert = _key_and_cert(tmp_path)
    base = "https://saml-idp.example.com"
    settings: dict[str, Any] = {
        "entityid": f"{base}/metadata",
        "xmlsec_binary": saml.xmlsec_binary(),
        "key_file": key,
        "cert_file": cert,
        "service": {
            "idp": {
                "endpoints": {
                    "single_sign_on_service": [(f"{base}/sso", BINDING_HTTP_REDIRECT)],
                    "single_logout_service": [(f"{base}/slo", BINDING_HTTP_REDIRECT)],
                },
                "name_id_format": [NAMEID_FORMAT_PERSISTENT],
                "policy": {"default": {"lifetime": {"minutes": 5}, "name_form": NAME_FORMAT_BASIC}},
            }
        },
    }
    config = IdPConfig()
    config.load(dict(settings))
    provider = IdentityProvider.objects.create(
        name="Acme SAML",
        slug="acme",
        kind="saml",
        enabled=True,
        domains=["saml.example"],
        config={
            "idp_metadata": create_metadata_string(None, config=config).decode(),
            "email_attribute": "mail",
            "name_attribute": "displayName",
            "groups_attribute": "groups",
        },
    )
    sp_metadata = client.get("/sso/acme/saml/metadata/", secure=True)
    assert sp_metadata.status_code == 200 and b"AssertionConsumerService" in sp_metadata.content
    settings["metadata"] = {"inline": [sp_metadata.content.decode()]}
    config = IdPConfig()
    config.load(settings)
    return Server(config=config), provider


def _saml_response(
    server, request_url: str, *, email="carol@saml.example", sign=True, in_response_to=None
):
    import base64

    from saml2 import BINDING_HTTP_REDIRECT
    from saml2.saml import NAMEID_FORMAT_PERSISTENT, NameID

    query = parse_qs(urlsplit(request_url).query)
    request = server.parse_authn_request(query["SAMLRequest"][0], BINDING_HTTP_REDIRECT)
    response = server.create_authn_response(
        identity={"mail": [email], "displayName": ["Carol"], "groups": ["ops"]},
        in_response_to=in_response_to or request.message.id,
        destination="https://testserver/sso/acme/saml/acs/",
        sp_entity_id="https://testserver/sso/acme/saml/metadata/",
        name_id=NameID(format=NAMEID_FORMAT_PERSISTENT, text="c-1"),
        authn={"class_ref": "urn:oasis:names:tc:SAML:2.0:ac:classes:Password"},
        sign_assertion=sign,
    )
    return base64.b64encode(str(response).encode()).decode()


# pysaml2 leaves its xmlsec1 temp files to the garbage collector (deleted, but reported on 3.14).
SAML_TEMP_FILES = pytest.mark.filterwarnings(
    "ignore:Implicitly cleaning up:ResourceWarning",
    "ignore::pytest.PytestUnraisableExceptionWarning",
)


@SAML_TEMP_FILES
def test_saml_sign_in_and_replay(saml_idp, client: Client) -> None:
    server, provider = saml_idp
    group = Group.objects.create(name="Ops")
    provider.group_mappings = {"ops": str(group.pk)}
    provider.save()
    start = client.get("/sso/acme/login/", secure=True)
    assert start.status_code == 302 and start["Location"].startswith(
        "https://saml-idp.example.com/sso?SAMLRequest="
    )
    from patchmanager.sso import saml

    assert start.cookies[saml.COOKIE]["samesite"] in ("Lax", "None")
    posted = _saml_response(server, start["Location"])
    response = client.post("/sso/acme/saml/acs/", {"SAMLResponse": posted}, secure=True)
    assert response.status_code == 302 and response["Location"] == "/", response.content
    user = User.objects.get(email="carol@saml.example")
    assert ExternalIdentity.objects.get(user=user, provider=provider).subject == "c-1"
    assert list(user.pm_groups.all()) == [group]
    replay = client.post("/sso/acme/saml/acs/", {"SAMLResponse": posted}, secure=True, follow=True)
    assert b"expired" in replay.content or b"could not be verified" in replay.content
    logout = client.post("/auth/logout/", secure=True)
    assert logout["Location"].startswith("https://saml-idp.example.com/slo?SAMLRequest=")
    assert client.get("/sso/acme/saml/sls/", secure=True).status_code == 302


@SAML_TEMP_FILES
def test_saml_rejects_unsigned_forged_and_unsolicited(saml_idp, client: Client) -> None:
    server, _provider = saml_idp
    start = client.get("/sso/acme/login/", secure=True)
    unsigned = _saml_response(server, start["Location"], sign=False)
    response = client.post(
        "/sso/acme/saml/acs/", {"SAMLResponse": unsigned}, secure=True, follow=True
    )
    assert b"could not be verified" in response.content
    start = client.get("/sso/acme/login/", secure=True)
    import base64

    good = base64.b64decode(_saml_response(server, start["Location"]))
    forged = base64.b64encode(good.replace(b"carol@saml.example", b"admin@saml.example")).decode()
    response = client.post(
        "/sso/acme/saml/acs/", {"SAMLResponse": forged}, secure=True, follow=True
    )
    assert b"could not be verified" in response.content
    stranger = Client()
    unsolicited = stranger.post(
        "/sso/acme/saml/acs/",
        {"SAMLResponse": base64.b64encode(good).decode()},
        secure=True,
        follow=True,
    )
    assert b"expired" in unsolicited.content
    assert not User.objects.filter(email__endswith="@saml.example").exists()
