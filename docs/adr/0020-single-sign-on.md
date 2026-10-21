# 20. Single sign on: OIDC, SAML 2.0 and LDAP

* Status: accepted
* Date: 2026-10-21

## Context

Organisations want their users to sign in with the identity provider they
already run, keep group membership there and turn off local passwords, without
locking themselves out when the provider fails.

## Decision

* **Providers** (`sso.IdentityProvider`) are instance wide and managed only by
  super admins (`system.sso.manage`). Each has a kind, the email domains it is
  responsible for, and switches for enforcement, account creation on first
  sign in (JIT) and trusted MFA. Secrets (OIDC client secret, LDAP bind
  password) are encrypted at rest.
* **OIDC**: authorization code flow with PKCE (S256), `state` and `nonce`, the
  client authenticates with `client_secret_basic`. Discovery and keys come from
  the configured issuer (cached one hour, keys refetched once on an unknown key).
  ID tokens are verified with joserfc: asymmetric algorithms only, issuer,
  audience (and `azp` with several audiences), expiry, issue time and nonce.
  Unverified email addresses are refused unless the provider is configured to
  trust them. RP initiated logout uses `end_session_endpoint`. authlib was
  considered; its JOSE module is deprecated in favour of joserfc, and the code
  flow is small enough to keep explicit.
* **SAML 2.0**: pysaml2 with xmlsec1 (python3-saml needs a libxml2 that conflicts
  with PostgreSQL 18's ICU in our solve group). Signed assertions are required,
  unsolicited responses refused, clock skew one minute. Because the IdP posts the
  response cross site, where the Lax session cookie is not sent, outstanding
  request ids travel in their own signed, HttpOnly, `SameSite=None` cookie valid
  ten minutes; an answered id is removed, so responses cannot be replayed. IdP
  metadata is pasted, not fetched. SP initiated single logout uses the HTTP
  Redirect binding. pysaml2's verbose loggers are capped at WARNING because they
  log whole assertions.
* **LDAP**: python-ldap (ldap3 is unmaintained since 2021). Service bind, search
  with an escaped filter (at most one entry), then a bind as the person; empty
  passwords are refused (an unauthenticated bind would succeed); certificates are
  verified, plain `ldap://` needs StartTLS, referrals are not followed. LDAP
  replaces the local password for the provider's domains in the normal form.
* **Accounts**: a provider identity (`sub`, NameID, entry id) is linked to one
  user. A first sign in links an existing account only if the email's domain is
  one the provider is responsible for (otherwise an attacker controlled IdP
  could take over accounts); otherwise JIT creates the account (no password) or
  the sign in is refused. Deactivated, locked and service accounts never sign in.
* **Groups**: mappings from external group values to local groups. Mapped groups
  follow the provider on every sign in (added and removed); other memberships
  stay. Roles still come from assignments to those groups.
* **MFA**: unless the provider is marked as enforcing MFA, users with a second
  factor must still enter it, and users who need MFA must enrol one.
* **Enforcement**: for enforced domains the password form redirects to the
  provider (domain discovery). Accounts marked *break glass* by a super admin
  keep their password for emergencies.

## Consequences

* SAML signing of AuthnRequests and encrypted assertions are not offered yet.
* LDAP group membership comes from an attribute (for example `memberOf`); nested
  directory groups are not resolved.
