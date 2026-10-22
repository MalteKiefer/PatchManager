# 29. Switching, routing and reading switch configurations

* Status: accepted
* Date: 2026-10-22

## Context

Network teams document more than VLANs: link aggregations, spanning tree, VLAN
interfaces with addresses, static routes, OSPF, BGP, switch stacks and virtual
switches (VSF, VSX, MLAG), tunnelled ports and device profiles. Typing all of that
by hand is slow and drifts from the running configuration. The configurations are
already stored per device (ADR 0027).

## Decision

* Ports gain link aggregation membership and protocol, MTU, tunnelling, edge port,
  BPDU and root guard, OSPF area and passive. Port kinds gain `lag` and `virtual`
  (VLAN interfaces, loopbacks); a VLAN interface is a virtual port named `vlan <id>`
  and carries its addresses through IPAM.
* New per device records, governed by the device's permissions, in the device's
  tenant (row level security and triggers): switch settings (hostname, spanning
  tree mode and priority, MST region, IP routing, BGP AS and router ID, OSPF router
  ID, tunnel gateways), MST instances, static routes, BGP neighbors and device
  profiles. Stacks belong to a site and list member switches with number, role and
  priority. All of them are in the history and move with their site.
* A parser reads ArubaOS-Switch (AOS-S, ProCurve) configurations and Aruba Central
  templates (`%variable%`, `%if%`/`%else%`/`%endif%`, filled from a JSON variables
  file). It reads VLANs and their ports, VLAN addresses, trunks, port names and
  states, spanning tree, static routes and default gateway, OSPF, BGP, device
  profiles, tunnelled node servers and stacking/VSF members. Secrets (passwords,
  keys, SNMP communities) are never read; their lines are only counted. Unknown
  commands are listed, not guessed. Lines over 2000 characters, port ranges over
  1024 ports and port names over 64 characters are refused, which also keeps the
  parser linear.
* "Read a configuration" takes pasted text, a file or a stored version, shows the
  changes it would make (the same work in a rolled back transaction) and applies
  them on confirmation. Documented VLAN names win over names in the configuration;
  ports the device does not have are reported, not created; VLANs missing at the
  site are created. Routes, BGP neighbors, MST instances and profiles are replaced by
  the configuration's lists.
* The documentation of a device can be written back as an ArubaOS-Switch
  configuration; reading that output again gives the same documentation.
* The switching page of a device shows a port matrix coloured by untagged VLAN with
  tagged VLANs, edge ports, guards and tunnelling, the VLAN table, routing, spanning
  tree, link aggregations, stack and profiles.

## Consequences

* Other platforms (AOS-CX, Cisco IOS) need their own parser into the same
  structure; the apply step stays the same.
* Stacks are maintained by hand: a configuration names member numbers but not which
  documented switch is which member.
