# 18. History, search, import and export

* Status: accepted
* Date: 2026-10-21

## Decision

### Change history

* **Database triggers** write the history (`history.Change`): one row per insert,
  update and delete of sites, buildings, floors, rooms, racks, devices, ports,
  cables, VLANs and WLANs, with the row before and after as JSON and the changed
  columns. Triggers also catch bulk `QuerySet.update()` calls and cascades that
  model signals would miss. Updates that change nothing (or only timestamps and
  the derived path) write no row.
* The request middleware puts the actor, its label (with the impersonating admin)
  and the request id into session settings the trigger reads. Background work
  without a request is recorded as *system*.
* Each row keeps the object's policy scope (ltree path) at that time. Reading
  history needs the view permission of the object type on that scope; it also
  works after the object was deleted. Grant conditions refer to object fields and
  cannot be judged on history rows, so conditional allows are ignored and
  conditional denies apply to their whole scope (fail closed).
* History is tenant isolated by RLS and deleted with its tenant. It is not the
  audit log: the audit log stays the tamper evident security record; history is
  the working view for users.
* **Restore** sets the changed plain fields of one update back. It needs the
  change permission on the current object and on the resulting state, and is
  refused when one of the fields changed again since (no silent overwrite).
  Links (room, rack, VLAN) and deletions are not restored: moves and re-creation
  have side effects (rack space, ports, cable scopes) that belong to the domain
  services. The restore itself is a new history entry.

### Search

* One search over names, codes, serial numbers, asset tags, port labels and MAC
  addresses, cable labels, VLAN names and IDs and WLAN SSIDs, using `icontains`
  plus trigram word similarity (`pg_trgm`, GIN indexes), ranked by similarity,
  so typos still match. Notes use full text search with the `simple`
  configuration, which works for German and English alike.
* Results are limited to the current tenant and to objects the user may view;
  ports count as visible when their device is. Facets show the number of visible
  hits per type (capped at 200 candidates per type).
* Saved searches are per user (name, term, type), at most 50.

### Import

* CSV (UTF-8, comma, semicolon or tab) and XLSX (first sheet), at most 5000 rows,
  cells up to 1000 characters, workbooks up to 100 MB uncompressed.
* Tables: locations, racks, devices, VLANs and cables. Every row goes through the
  domain services, so permissions, validation, audit and history apply. Existing
  objects are matched by their natural key (name within the parent, device name
  within the tenant, VLAN ID within the site, the two cable ports) and updated;
  empty cells keep current values. Only the locations table creates missing
  parents; other tables refuse unknown locations to avoid typos creating places.
* The whole file runs in one transaction. The first pass always rolls back (dry
  run) and shows the result per line; *Import now* runs it again for real and
  imports nothing if any row fails. The parsed rows wait 30 minutes in the cache,
  bound to the user and tenant.

### Export

* Tables (the import columns plus a read only port list) as CSV, XLSX and JSON,
  for the tenant or one site, limited to objects the user may view. Text cells
  starting with `= + - @`, tab or carriage return get a leading apostrophe
  (OWASP CSV injection); the importer removes it again, so exports round trip.
* PDF reports with reportlab (BSD): site report, rack elevation, port list,
  patch list and bill of materials, with the instance name, the user and the time
  in the footer and an optional operator logo (`PATCHMANAGER_BRAND_LOGO`, PNG or
  JPEG up to 2 MB; a broken setting fails the system check). Hidden peers and
  devices are shown as *not visible*.
* Exports run synchronously, at most 20 000 rows per table and 60 exports per user
  in 10 minutes. Background export jobs follow when larger data sets need them.

## Consequences

* Every tracked write costs one extra insert; bulk imports write one history row
  per object and port.
* Restoring links and deleted objects needs a later, service based design.
