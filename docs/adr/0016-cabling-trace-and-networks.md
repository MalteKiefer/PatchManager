# 16. Cabling, path tracing, VLANs and WLANs

* Status: accepted
* Date: 2026-10-21

## Decision

* **Cables** join exactly two ports (`cabling.CableEnd`, one row per side, a
  port carries at most one end). Kinds: *permanent link* (horizontal cabling,
  typically rear port to rear port or to an outlet) and *patch cord*. Media,
  colour, length, label and status are attributes. The database refuses ends
  whose port or cable belongs to another tenant, so cables never cross tenants;
  inside a tenant they may join racks, rooms, floors, buildings and sites.
* A cable's permission scope is the deepest location both ends share. Creating
  a cable needs `cabling.cable.add` on both end devices.
* **Tracing** walks from a port over its cable; at a front port it crosses to
  the mapped rear port and position, at a rear port it leaves through the front
  port mapped to the position it entered with. It stops at an active port, an
  open end or a revisited port (loop). Passive start ports are traced in both
  directions. A path is *complete* when both ends are active ports. Property
  based tests check that every start point on random panel chains yields the
  same two ends and that tracing from either end yields the reversed path.
  Viewers may trace (`*.trace` added to Viewer and Auditor); ports on devices
  the viewer cannot see are shown anonymised.
* **VLANs** are tenant wide or bound to a site; VLAN ID unique per scope. Ports
  are `access` (one untagged VLAN) or `trunk` (optional native VLAN plus tagged
  VLANs). A trigger accepts only VLANs of the port's tenant and, for site
  VLANs, the port's site.
* **WLANs** (SSID, security, optional VLAN) are broadcast by wireless ports of
  access points; the database accepts only wireless ports of the same tenant.
* Moving a site to another tenant is refused while cables leave the site or its
  ports use tenant wide VLANs or WLANs; otherwise cables, site VLANs and tagged
  assignments move with it.
