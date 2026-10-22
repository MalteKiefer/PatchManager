# 33. Limits for API tokens and clients

* Status: accepted
* Date: 2026-10-22

## Context

API tokens (ADR 0019) act with all permissions of their user, narrowed only to read
or write. Integrations usually need much less: a monitoring tool reads devices of
one site, a backup tool writes configurations. A leaked token should do no more
than its job.

## Decision

* Personal tokens and OAuth clients can be limited to object types (for example only
  `dcim.device` and `ipam.ipaddress`) and to one location (a site, building, floor or
  room and everything in it). OAuth access tokens take the limits of their client.
* The limits are enforced in the policy layer: authentication attaches them to the
  request's user, and every permission check and every scoped query of the request
  applies them before the user's own grants. They narrow super admins too.
* A read only token is limited to view permissions in every check, not only by HTTP
  method. With a location limit, global objects such as library types can only be
  read.
* Limits are checked when the token is created: known object types only, and a
  location the creator may see.

## Consequences

* A token is never more powerful than its user and its limits together; changing the
  user's roles still changes what the token can do inside its limits.
