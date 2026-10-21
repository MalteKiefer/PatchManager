# Feature scope (clean room notes)

Behaviour observed on the public pages of a commercial rack documentation service
(features, pricing, security pages and its public documentation), reviewed on
2026-10-21. Written in our own words. No code, text, UI or assets were copied. Where
PatchManager deliberately differs, the difference is noted.

## Observed behaviour

* **Hierarchy:** account, location (address, map), floor (one 2D plan, not necessarily a
  storey), room (rectangle or polygon, rooms may overlap), rack, device. Devices outside
  any room fall into an implicit default room. Each object has a short identifier that is
  unique within its parent, giving full paths such as `HQ/2/SR/R01/SW02/13`.
  *PatchManager adds an explicit building level between site and floor.*
* **Rack editor:** add a device by clicking a free unit, front and rear view, rear ports
  block placement behind them, tray devices for non rack mounted equipment.
* **Ports:** wired (connector type such as RJ45, LC, E2000) or wireless (client or
  broadcast). Names are unique per device; renaming keeps connections.
* **Connections:** two kinds. A permanent link (horizontal cabling between outlets and
  patch panel ports, also across rooms, floors and buildings) and a movable patch cord.
  Patch cords attach to a panel port only once its permanent link exists. Each
  connection has a colour; cassette based panels carry type, length and colour per slot.
* **Trace:** lists every hop from end to end through panels and outlets. A path is
  complete when both ends reach an active device; otherwise incomplete.
* **Networks:** VLANs assigned per switch port; WLANs linked to wireless ports.
* **Metadata:** notes, photos and a responsible person on locations, floors, rooms,
  racks and devices.
* **Activity log** with user and timestamp, CSV export.
* **Export:** PDF for tenant, location, floor, room, device or rack scope with cover,
  table of contents, rack front and rear, connection paths and used VLANs; rack image;
  bill of materials; CSV for resources, racks, VLANs, WLANs, users and activity.
  Limits on devices per report and exports per minute.
* **Tenants** (integrator tier): customer with identifier, address and contact; users can
  be restricted to tenants; locations can be moved between tenants.
* **Roles:** read everything, or admin at account, tenant, location, floor, room or device
  scope, inheriting downward; one owner per organisation.

## Tiers (to be reproduced as configurable presets, Phase 8)

| Preset | Racks | Users | Tenants | Connections | PDF |
|---|---|---|---|---|---|
| Free | 3 | 1 | 1 | within one rack | BOM only |
| SMB | unlimited | unlimited | 1 | all | yes |
| Integrator | unlimited | unlimited | up to N (default 100) | all | yes |
| Enterprise | unlimited | unlimited | unlimited | all | yes |

## Deliberate extensions

* Fine grained permissions with groups, deny rules and conditions (the reference has two
  roles).
* SSO (OIDC, SAML) and SCIM; the reference mentions MFA only.
* REST API and webhooks; no public API was found in the reference material.
* Explicit patch panel front to rear port mapping; the reference does not document it.
* Self hosted, no third party processors by default.

## Assumptions

Recorded as ADRs when the affected phase starts. Open points: VLAN scope (we allow
tenant wide and site scoped), cable length warning versus rack distance (warning, not
blocking), report size limits (configurable).
