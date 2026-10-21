# 14. Location tree and rack space

* Status: accepted
* Date: 2026-10-21

## Decision

* The tree is `tenant > site > building > floor > room > rack`, one table per
  level (`locations.Site`, `Building`, `Floor`, `Room`, `dcim.Rack`). Each row
  stores its tenant and an `ltree` `path` of UUID labels below the tenant label.
  The path is both the permission scope and the subtree index (GiST).
* A trigger (`pm_check_hierarchy`) rejects any row whose tenant differs from
  its parent's or whose path is not `<parent path>.<own id>`. This makes cross
  tenant attachment and forged scopes impossible even for buggy code.
* Rack space is a separate table (`dcim.RackSpace`) holding a unit range and a
  face range (`[0,1)` front, `[1,2)` rear, `[0,2)` both). One exclusion
  constraint (`rack =, faces &&, units &&`, using `btree_gist`) forbids any
  overlap; triggers keep spaces inside the rack and in the rack's tenant and
  stop a rack from shrinking below used units. In this release spaces are
  reservations; devices in the next release occupy space the same way.
* Moving a site to another tenant rewrites tenant ids and path prefixes of the
  whole subtree, its rack space and the role assignments scoped inside it, in
  one transaction.
* Assignments can be scoped to any node of the tree, not only to tenants.
* Maps use Leaflet with OpenStreetMap tiles loaded by the browser. The tile
  URL is configurable (`PATCHMANAGER_MAP_TILE_URL`, empty disables maps) and
  its origin is added to the CSP `img-src`. There is no server side geocoding:
  coordinates are entered or picked on the map, so addresses never leave the
  instance.

## Consequences

Browsers that show the map contact the tile server, which sees their IP
address. Operators who want no third party requests run their own tile server
or disable the map.
