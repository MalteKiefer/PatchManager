# 15. Device library, devices, ports and uploads

* Status: accepted
* Date: 2026-10-21

## Decision

* **Library:** vendors are shared. Device types are shared (no tenant, managed
  with global `library.*` permissions) or private to one tenant (custom
  devices). Row Level Security on types, port templates and attachments uses a
  shared variant: rows without tenant are readable by all, writable only with
  all tenant visibility.
* **Port templates** describe groups of ports: a name pattern where `{n}` or
  `{n:02}` is the only placeholder (no `str.format`, so patterns cannot reach
  Python attributes), count, start, kind, connector and speed. Front ports of
  passive gear map to rear ports in order (one to one, or many fronts into the
  positions of fewer rears, for example 12 LC into one MPO), or explicitly by
  rear port name and position for imported definitions.
* **Devices** are tree nodes below their rack (mounted) or room (floor
  device). A database trigger keeps the device in its parent's tenant, its rack
  in its room, its path consistent, and forbids types private to another
  tenant. Mounted devices take rack space like reservations (exclusion
  constraint); full depth devices occupy both faces.
* **Ports** are created from the templates when the device is created and are
  documented individually afterwards. Changing a template does not touch
  existing devices; the shape (form, height, depth) of a type in use is locked.
* **Interchange:** YAML/JSON in the community device type library format
  (`manufacturer`, `model`, `u_height`, `interfaces`, `front-ports`,
  `rear-ports`, `console-ports`, `power-ports`). Runs of numbered names are
  folded into patterns on import. Only files supplied by users are processed;
  no third party library is bundled, so its license does not apply to this
  repository.
* **Uploads:** accepted by content, not by name or declared type: JPEG, PNG,
  GIF, WebP (decoded and re-encoded, EXIF and trailing data dropped,
  decompression bomb limit), SVG (parsed with defusedxml, rebuilt from an
  element and attribute allow list, external references removed) and PDF
  (served only as download). Size limit `PATCHMANAGER_UPLOAD_MAX_MB` (20). An
  optional clamd scan (`PATCHMANAGER_CLAMD_HOST`) fails closed when the scanner
  is unreachable.
* **Notes** are Markdown with raw HTML disabled, rendered and then sanitised
  with nh3 (allow listed tags, `http`/`https`/`mailto` links, `rel=noopener`).
