# 31. Configuration readers for many vendors

* Status: accepted
* Date: 2026-10-22

## Context

ADR 0029 added reading ArubaOS-Switch configurations. Networks mix vendors: Cisco,
Aruba AOS-CX, Fortinet, Juniper, MikroTik, HPE Comware, H3C, Huawei and LANCOM.
Their configurations describe the same things (VLANs, ports, aggregations, routes,
DHCP, DNS) in different syntax, and some describe VLAN membership per port, others
per VLAN.

## Decision

* One vendor neutral model (`networks.platforms.common.Config`). Every reader fills
  it; a shared step turns per port settings (access VLAN, native and allowed VLANs,
  aggregation membership) into per VLAN port lists, so applying a configuration is
  the same for every vendor.
* Readers: ArubaOS-Switch (and Aruba Central templates), AOS-CX, Cisco IOS, IOS-XE
  and NX-OS, FortiOS (nested config blocks), Junos (set commands and the curly
  brace format), RouterOS exports, Comware/H3C and Huawei VRP, and LANCOM LCOS
  scripts (tables matched by the column names of their `tab` line, since column order
  differs between firmware versions; only scripts in English are read).
* The platform is recognised from the text (each reader scores how sure it is) or
  chosen on the page.
* Applying also documents DNS servers and the domain, routed port addresses, routes
  that point at an interface or remote site, and DHCP pools as DHCP scopes of
  prefixes that are already documented, with reservations and relay addresses.
  Interface names match in their canonical form (`Gi1/0/1` is
  `GigabitEthernet1/0/1`).
* Secrets are never read: any line, statement or column that names a password, key,
  secret, community or hash is skipped and counted. Every reader shares the same
  limits (size, lines, line length, port names) and counts every port and VLAN
  membership against one budget per configuration, so crafted input cannot make the
  work grow beyond linear.

## Consequences

* Writing configurations back stays ArubaOS-Switch only.
* Firewall policies, VPNs, QoS and wireless controller settings are listed as not
  documented rather than guessed.
