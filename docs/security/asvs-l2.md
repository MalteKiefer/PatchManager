# ASVS Level 2 self-audit

Release 1.0.0, audited against OWASP ASVS 5.0, Level 2. This is a self-audit by the
maintainers, not an independent assessment. Each chapter was reviewed against the
code, the tests and the threat model; the evidence column names where a control
lives. Requirements were checked per chapter, not one by one with a formal record,
so treat a "met" as "no gap found", not as a certificate.

Status: **met** (no gap found), **partly** (gap listed below), **n/a** (feature not
present).

| Chapter | Status | Evidence |
|---|---|---|
| V1 Encoding and sanitization | met | Django auto escaping in every template; no `mark_safe` on user data; parameterised ORM queries only; CSV export neutralises formulas; SVG uploads sanitised with defusedxml and re-serialised (`metadata/uploads.py`) |
| V2 Validation and business logic | met | Django forms and DRF serializers at every entry point; services re-check invariants (rack space exclusion constraint, port and cable integrity in the database); import validates the whole file before writing; rate limits on login, MFA, SSO, API, imports |
| V3 Web frontend security | met | Strict CSP without `unsafe-inline` or `unsafe-eval` (Alpine CSP build); `frame-ancestors 'none'`, `X-Frame-Options: DENY`; `__Host-` cookies, `SameSite=Lax`, `HttpOnly`, `Secure`; HSTS two years (preload opt-in); COOP `same-origin`; `nosniff`; vendored scripts, no third party origins except an optional tile server |
| V4 API and web service | met | Token, OAuth client credentials and SCIM authentication; scopes per token; object level checks through the same policy engine as the UI; RLS below it; OpenAPI 3.1 schema; per user rate limits; JSON only |
| V5 File handling | partly | Size limits (app and Caddy), type sniffing, image re-encoding, SVG sanitising, encrypted storage, optional clamd scan, no execution path. Gap: PDF floor plans are rasterised with PDFium inside the web process, with a pixel cap and a lock but no sandbox |
| V6 Authentication | partly | Argon2id, minimum 12 characters, common and similar password checks, lockout per account and rate limit per client address, TOTP, WebAuthn, recovery codes, MFA enforcement per role and tenant, break glass accounts, password change needs the current password and ends other sessions. Gap: no check against a breached password corpus (only Django's common password list) |
| V7 Session management | met | Server side sessions, key rotated at login, idle timeout 30 minutes, absolute lifetime 12 hours, list and revoke sessions, revocation on password change, MFA reset and deactivation, SCIM deprovisioning |
| V8 Authorization | met | Deny by default policy engine with allow and deny assignments, scopes on the location tree and conditions; every view and service checks the object; PostgreSQL RLS with `FORCE` as a second layer; hidden objects answer 404; tests for IDOR across tenants |
| V9 Self-contained tokens | met | OIDC ID tokens verified with joserfc (signature, issuer, audience, expiry, nonce, allowed algorithms); the application issues no self-contained tokens (API tokens are opaque and stored as HMAC) |
| V10 OAuth and OIDC | partly | OIDC client: authorization code with PKCE, state, nonce, exact redirect URI. OAuth server: client credentials only, Argon2 hashed secrets, short lived opaque tokens. Gap: SAML signed AuthnRequests and encrypted assertions are not offered (responses and assertions must be signed) |
| V11 Cryptography | met | Fernet (AES-128-CBC with HMAC-SHA256) for MFA secrets and uploads with key rotation; HMAC-SHA256 for tokens and webhook signatures; `secrets` for all random values; no custom primitives |
| V12 Secure communication | met | TLS 1.2 and 1.3 at Caddy, HTTP redirected; internal network for app, database and Redis; webhooks over https only by default with an SSRF guard that pins the resolved address and rejects private, loopback and IPv4 mapped addresses |
| V13 Configuration | met | Distroless, read only, non root image without shell; `cap_drop: ALL`, `no-new-privileges`; secrets from files; `check --deploy` in CI; signed images with SBOM and provenance; pinned dependencies (pixi lock), pip-audit, Trivy, CodeQL, gitleaks; health and metrics not exposed by the proxy |
| V14 Data protection | met | Tenant isolation in the database; personal data kept to account fields; sensitive values never logged (audit URL redaction, SAML logs above WARNING only); backups encrypted at host level (documented); `Cache-Control: no-store` on authenticated pages through Django defaults for sessions |
| V15 Secure coding and architecture | met | Threat model per phase, ADRs, dependency review before adding (ldap3 and python3-saml rejected), Renovate configuration, bandit, mypy strict, 90 % coverage gate |
| V16 Security logging and error handling | met | Append only audit log with hash chain and verifier; JSON logs with request ids; client address from trusted proxies only; generic error pages; failures of security controls fail closed (RLS context, policy engine, uploads with clamd) |
| V17 WebRTC | n/a | Not used |

## Gaps and decisions

| Gap | Risk | Decision |
|---|---|---|
| No breached password check | Users can pick a password that is known from breaches but not in the common list | Accepted for 1.0: a lookup needs an external service or a large offline corpus. Instances that need it enforce SSO or MFA for everyone (`PATCHMANAGER_MFA_REQUIRED`) |
| PDFium in the web process | A PDFium memory safety bug could be reached with a crafted PDF by a user who may upload floor plans | Accepted for 1.0: upload needs a floor plan permission, the pixel cap and lock limit resource use. Moving rasterising to a sandboxed worker is planned |
| SAML without signed requests and encrypted assertions | Assertions travel readable through the browser (inside TLS) | Accepted: signed responses are required, IDs are single use. OIDC is the recommended protocol |

## Found and fixed during the audit

* Behind the proxy every request carried the proxy's address. The login rate limit per
  IP was therefore shared by all users, and one client could block sign in for
  everyone. Fixed by taking the client from `X-Forwarded-For` of trusted proxies only.
* Editions and their feature gates were removed (ADR 0023), which removes a class of
  checks that could fail open.

## Not covered

Infrastructure outside the compose stack (host hardening, firewall, backup storage
encryption, identity provider configuration) is the operator's responsibility. A manual
penetration test against a running instance follows the [checklist](pentest-checklist.md).
