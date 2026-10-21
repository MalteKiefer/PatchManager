# 12. Authentication, MFA and sessions

* Status: accepted
* Date: 2026-10-21

## Decision

* Email login, Argon2id hashes (Django's Argon2 hasher), password policy via
  Django validators with a configurable minimum length (default 12).
* Brute force: per account lockout after `PATCHMANAGER_LOGIN_MAX_FAILURES`
  failures for `PATCHMANAGER_LOGIN_LOCKOUT_SECONDS`; per IP window limit for
  password and WebAuthn attempts; five second factor attempts per login.
  Unknown emails cost the same hashing time as known ones.
* Second factors: TOTP (RFC 6238, seed encrypted with Fernet keys from
  `PATCHMANAGER_ENCRYPTION_KEYS`, one use per time step), WebAuthn security keys
  and passkeys (py_webauthn; passkeys with user verification also allow
  passwordless sign in), ten single use recovery codes stored as HMACs.
* MFA is required when configured globally, by any assigned role
  (`requires_mfa`, always on for Super Admin) or by any tenant the user can
  access. Such users can only reach the security page until a factor exists.
* Server side sessions in `accounts.UserSession` with owner, address, user agent
  and timestamps, listed and revocable by the user and by admins. Absolute
  lifetime 12 h and idle timeout 30 min by default. Password changes, locks,
  deactivation and MFA resets end other sessions.
* First run: no default credentials. `python -m django setup_token` prints a one
  hour token; the setup page creates the first Super Admin with it and is
  disabled once any user exists.
* Impersonation (assumption: "consent" means visible and accountable, not a
  prompt to the target user): Super Admin only by default, requires a reason,
  never targets super admins, lasts at most 30 minutes, shows a banner on every
  page, start and stop are audited.

## Consequences

Losing all factors and recovery codes requires an admin MFA reset, which is
audited. A consent prompt to the impersonated user can be added later without
changing the session model.
