# 6. Server rendered UI with HTMX, Alpine.js and Tailwind CSS

* Status: accepted
* Date: 2026-10-21

## Context

The UI is form and table heavy, with a few highly interactive views (rack elevation,
floor plans, trace graphs). A strict CSP without inline scripts is required.

## Decision

Django templates, HTMX 4 for partial updates, Alpine.js (CSP build) for local state,
Tailwind CSS 4 compiled at build time, and plain SVG plus small ES modules for the
interactive views. Leaflet with OpenStreetMap tiles for maps. All assets are self hosted;
no CDN.

## Consequences

No JavaScript build chain beyond the Tailwind CLI. Interactive views keep state on the
server and exchange HTML fragments or JSON.
