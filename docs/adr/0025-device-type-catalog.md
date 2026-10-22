# 25. Device type catalog from the community library

* Status: accepted
* Date: 2026-10-22

## Context

Typing in a switch with 52 ports or a patch panel with front to rear mapping by
hand is slow and error prone. The community device type library
(github.com/netbox-community/devicetype-library) describes more than 12 000
device types in YAML and is released into the public domain (CC0-1.0), so it may
be bundled with AGPL software. Its repository also holds about 1 GB of images.

## Decision

* `scripts/catalog.py` takes a sparse checkout of the YAML definitions only and
  writes `library/catalog.json.gz` (about 0.4 MB) with the source commit. The file
  is committed, so the catalog works without internet access and is reviewed like
  any other change.
* After migrations the file is loaded into `library.CatalogType` when its commit
  differs from the loaded one.
* The library offers *Add from catalog*: search by manufacturer, model or part
  number, a preview of every port, and a copy into the library, shared or for the
  current tenant, through the same importer as uploaded YAML files. Logical
  interfaces (LAG, virtual) are skipped; rear ports named like front ports get an
  `R` prefix because port names are unique per device.
* Images are not bundled.

## Consequences

* Refreshing the catalog is a reviewed commit (`pixi run -e dev python scripts/catalog.py`).
* Copied types are ordinary library types and can be changed freely.
