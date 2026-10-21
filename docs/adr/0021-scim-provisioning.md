# 21. SCIM 2.0 provisioning

* Status: accepted
* Date: 2026-10-21

## Decision

* SCIM 2.0 (RFC 7643, 7644) under `/scim/v2/`: `Users`, `Groups`,
  `ServiceProviderConfig`, `ResourceTypes` and `Schemas`, with
  `application/scim+json` and RFC error responses. Plain Django views keep the
  protocol details (content type, error format, PatchOp) explicit.
* **Authentication**: bearer tokens `pmsc_<prefix>_<secret>` created by super
  admins (`system.scim.manage`), stored as keyed HMAC like API tokens, revocable,
  rate limited per token (600 requests per minute), optionally limited to email
  domains (for instances shared by several organisations).
* **Scope**: SCIM only sees and changes accounts it created (`scim_provisioned`);
  existing local accounts answer `uniqueness`. A provisioning client therefore
  cannot take over accounts administrators made, and changing an address also
  removes the account's SSO links and sessions.
* **Users**: `userName` is the email address (case insensitive, unique);
  `displayName` or `name`, `emails`, `active` and `externalId` are mapped.
  Setting `active` to false deactivates the account and ends its sessions; API
  tokens stop working with it. SCIM only reactivates accounts it deactivated
  itself, never ones an administrator deactivated or locked. `DELETE` deprovisions:
  the account is deactivated and hidden from SCIM but kept for the audit trail,
  and its roles, groups, second factors, API tokens, SSO links and sessions are
  removed, so creating the same `userName` again starts clean. SCIM users have no password
  and sign in through SSO.
* **Groups**: `displayName`, `externalId` and user `members`. Nested groups are
  not provisioned. SCIM only sees and changes groups it created
  (`scim_managed`); administrators give those groups roles. So a provisioning
  client decides who is in which group, never what a group may do.
* **Safeguards**: super admins (judged by their assignments, also while locked or
  inactive), break glass accounts and API client service users can neither be
  changed nor added to or removed from groups by SCIM.
* **Filters**: `eq`, `ne`, `co`, `sw`, `ew`, `pr`, `gt`, `ge`, `lt`, `le`,
  `and`, `or`, `not`, parentheses and value paths such as
  `members[value eq "..."]`, on an allow list of attributes per resource, bounded
  in length and depth. Pagination with `startIndex` and `count` (at most 200);
  sorting and bulk operations are not offered.
* **PATCH**: `add`, `replace` and `remove` with or without `path`, including the
  forms Microsoft Entra ID (string booleans, capitalised ops) and Okta send.
* **Versioning**: weak ETags from the modification time; `If-Match` with a stale
  version is refused with 412.

## Consequences

* Roles of provisioned groups stay a decision of the administrators.
* Clients that rely on nested groups or sorting need a flat group design.
