# 17. Floor plans

* Status: accepted
* Date: 2026-10-21

## Context

Floors need a plan on which rooms, racks and floor devices (access points,
cameras, outlets) can be found. Plans arrive as photos, exports from CAD tools
or PDFs. The reference behaviour leaves open how PDFs and vector plans are
handled and how placement relates to the room a device belongs to.

## Decision

* One plan per floor (`floorplans.FloorPlan`), stored as an encrypted
  attachment of the floor through the existing upload pipeline (content
  sniffing, re-encoding, optional malware scan, permission checked download).
  Accepted: JPEG, PNG, WebP, GIF and PDF. Replacing the plan deletes the old
  image and resets the scale.
* **PDFs** are rasterised on upload with pypdfium2 (PDFium, Apache-2.0): first
  page only, 150 dpi, longest side at most 6000 pixels, then stored as PNG. The
  original PDF is not kept. PDFium is a large C parser; it runs inside the
  upload request, only on files below the upload limit. The malware scan of the
  attachment pipeline sees the resulting PNG, not the PDF. Moving rasterising to
  a sandboxed worker is a later hardening step.
* **SVG plans are refused.** The SVG sanitiser keeps attachments safe to view,
  but plan coordinates rely on known pixel sizes; users export vector plans as
  PNG or PDF instead.
* **Coordinates** are pixels of the stored image, rounded to 0.1. The scale is
  set from two clicked points and their real distance (pixels per metre). Rack
  and device symbols are sized from the scale (rack 0.6 m by its depth).
* **Room outlines** are polygons (3 to 64 points, rectangles included) stored on
  the room. Point in polygon uses ray casting; edges count as inside.
* **Placement**: racks keep their room; a rack dropped outside its room's
  outline is placed with a warning. A floor device dropped inside exactly one
  other room's outline moves to that room (needs `dcim.device.add` there), and
  cable scopes are recomputed. Overlapping outlines never move a device; the
  candidates are reported instead.
* Editing uses the same permissions as the objects: `locations.floor.change`
  for the plan and scale, `locations.room.change` for outlines,
  `dcim.rack.change` and `dcim.device.change` for placement. The editor shows
  only objects the user may view.
* The editor is an SVG drawn by a small module script without inline code
  (strict CSP). Items are focusable; arrow keys move them by 10 pixels (50 with
  Shift).

## Consequences

* No vector editing and no multi page plans; a building with several sheets
  uploads one per floor.
* Pixel coordinates stay valid as long as the image is not replaced; replacing
  a plan with a different size leaves positions to be checked by the user.
