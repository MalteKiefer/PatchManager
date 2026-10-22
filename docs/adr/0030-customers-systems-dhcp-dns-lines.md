# 30. Customers, systems, DHCP, DNS and lines

* Status: accepted
* Date: 2026-10-22

## Context

Documentation is read by people who ask "whose is this, what is it part of, how
does it get its address, what is its name and which line connects the site". Until
now devices, buildings and networks had no owner, no system, no DHCP or DNS data and
no provider lines, and devices had no warranty or support dates.

## Decision

* New app `directory`: organisations (customer, supplier, provider, service partner,
  internal department) with contacts, and systems (telephony, building automation,
  video and access control, printing, servers, applications ...) with customer,
  site, criticality and contact person.
* A link puts any documented object into a system or assigns it to an organisation
  in a role (customer, owner, operator, maintenance, supplier, user). Linkable are
  sites and the location tree, racks, devices, stored configurations, VLANs, WLANs,
  stacks, VRFs, prefixes, addresses, DHCP scopes, DNS zones and lines. Linking needs
  the change permission on the object and sight of the system or organisation; both
  must be in the same tenant.
* IPAM gains DHCP scopes (range in a prefix, gateway, DNS servers, domain, lease,
  relays, further options by code) with reservations by MAC address, and DNS zones
  with records (A, AAAA, CNAME, MX, TXT, PTR, SRV, NS, CAA). A and AAAA records point
  at the documented address with the same host. Scopes export as Kea JSON, zones as
  zone files (RFC 1035). Record values are one line and host targets are validated,
  so exports cannot carry extra records.
* New app `circuits`: lines with provider, kind, status, site, handover port,
  bandwidth, public addresses and contract (number, end, notice, cost, hotline).
* Devices gain supplier, warranty, support contract and end, and end of life.
* These kinds share one set of list, create and detail pages (`core.crud`), with the
  same permission checks as everywhere: the model's permission type on the object,
  on the tenant for lists and new records, on the parent for child records. All
  tables use row level security, a trigger that keeps every reference in the row's
  tenant, and the history.
* A site does not move to another tenant while systems, lines, suppliers, links or
  DHCP and DNS servers refer to it.

## Consequences

* Customers are organisations inside a tenant; tenants stay the security boundary.
* DHCP and DNS are documented, not served; the exports feed existing servers.
