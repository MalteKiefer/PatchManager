# 24. Personal data: export and anonymisation

* Status: accepted
* Date: 2026-10-22

## Context

Operators in the EU must answer access requests (GDPR Art. 15 and 20) and erasure
requests (Art. 17) for the people who use PatchManager. Personal data lives in
accounts (email, name, language), sessions (IP address, browser), second factors,
API tokens, sign in links, saved searches, the change history (author) and the audit
log (author, IP address, browser). The documented infrastructure itself is not
personal data, apart from free text such as a responsible person on a device.

The audit log is append only and hash chained (ADR 0012); rewriting entries would
break the chain that proves it was not tampered with.

## Decision

* **Export**: every signed in person can download their data as JSON from the
  profile page. It contains no secrets or hashes. Exports are audited and refused
  while an administrator impersonates the person.
* **Anonymise**: users with `accounts.user.delete` (and at least the rights of the
  target) can anonymise an account. Email and name are replaced, the password made
  unusable, the account deactivated; sessions, second factors, recovery codes, API
  tokens, sign in links, saved searches, invitations, groups and roles are deleted.
  Change history keeps its entries and shows the anonymous account as author. The
  account row stays so that history and audit references remain valid.
* **Audit log**: entries are kept unchanged as the security record (Art. 6(1)(f),
  Art. 17(3)(b) and (e)). Operators set a retention period in their records of
  processing and archive or prune old entries outside the application if required.
* Free text fields (notes, responsible person) are the operator's content; they are
  found with the search and edited by hand.

## Consequences

* Anonymisation cannot be undone; the UI asks for confirmation and the action is
  audited.
* SCIM or SSO may create a new, separate account if the person signs in again.
