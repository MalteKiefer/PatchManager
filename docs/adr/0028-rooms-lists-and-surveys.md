# 28. Rooms, editable lists and surveys

* Status: accepted
* Date: 2026-10-22

## Context

Rooms were only containers for racks. Teams document offices and meeting rooms too:
which outlets, access points and printers a room should have, which it has, where
each outlet is patched, and what a walk through the room found. Device categories
and room kinds were fixed lists in code, so nobody could add a kind their site uses.

## Decision

* Device categories (`library.DeviceCategory`) and room types
  (`locations.RoomType`) are tables that administrators edit under
  *Administration → Lists*. Each row has a stable key; the foreign keys point at the
  key, so stored values, imports and the API keep working. Built in rows can be
  renamed but not deleted; `other` is the default and always stays. A row in use
  cannot be deleted.
* A room type lists how many devices of each category a room of that type should
  have (`RoomTypeNeed`). A room can replace any of these numbers for itself
  (`RoomNeed`, tenant scoped). The room page compares planned against installed per
  category; installed counts only devices the viewer may see.
* The room page lists every device outside the racks with each port and the far end
  of its path (device, port and rack), traced only where the viewer may trace.
* The floor plan draws one line per pair of placed device and rack or device its
  paths end at, dashed for permanent links, with the number of paths. "Add device
  here" opens the device form with the room and the clicked point; the coordinates
  are validated on the server.
* Surveys (`RoomSurvey`, tenant scoped, governed by the room): date, who, status,
  notes, the count found per category, photos and notes through the attachment
  system. The survey page shows planned, documented and found side by side and marks
  differences. Changing surveys and plans needs `locations.room.change`.
* Deleting any object with attachments or notes now deletes them in the same
  transaction and removes the files after commit. Surveys and room plans move with
  their site to another tenant.

## Consequences

* Category and room type names given by administrators are not translated; built in
  names are.
* Survey counts are stored as a map from category key to number, so deleting a
  category leaves old survey numbers under a key that no longer shows.
