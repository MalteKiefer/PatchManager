# 27. Device configuration storage

* Status: accepted
* Date: 2026-10-22

## Context

Teams keep running configurations of switches, routers and firewalls next to their
documentation to see what changed and to rebuild a device. Configurations often hold
secrets: SNMP communities, password hashes, VPN keys.

## Decision

* New app `configs` with `DeviceConfig`: numbered versions per device. A version
  equal to the latest (same SHA-256 after normalising line endings) is not stored
  again. Text only, at most 2 MB.
* The text is encrypted at rest with the instance encryption keys (Fernet, as for
  uploads and second factors). Every read (page, download, diff, API) is recorded in
  the audit log.
* Own permissions (`configs.config.view`, `add`, `delete`) scoped to the device;
  the scope is stored and follows device and site moves. Row Level Security and a
  trigger keep each version in the tenant of its device.
* Pages: versions of a device, one version with line numbers, the changes between
  two versions, download, and a page to store a version by pasting text or choosing
  a file. The REST API lists and stores versions (`/api/v1/devices/<id>/configs/`),
  so backup tools can push configurations, for example nightly.
* Not built in: fetching configurations from devices (that needs device credentials
  and network access from the server); existing backup tools push through the API.

## Consequences

* Losing the encryption keys makes stored configurations unreadable, like uploads.
* Viewers who hold `*.view` see configurations; restrict with a deny rule on
  `configs.config.view` where needed.
