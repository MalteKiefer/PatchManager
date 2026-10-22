# 32. Servers, virtualisation and storage

* Status: accepted
* Date: 2026-10-22

## Context

Racks hold servers that run virtual machines, and NAS and SAN systems provide the
storage they use. Teams want to see which machines run where, keep that current
without typing it, and know which storage a machine uses. In a cluster, machines move
between nodes; a moved machine must stay one documented machine.

## Decision

* New app `compute`: clusters (Proxmox VE, vSphere, Hyper-V, KVM, XCP-ng, Nutanix,
  Kubernetes) with their hosts (documented server devices), virtual machines and
  containers with network cards and disks, and storage (pools, volumes, LUNs, NFS and
  SMB shares, datastores, buckets) of a storage device or a cluster. All tenant
  scoped, with row level security, same-tenant references and history; shown on the
  shared record pages and on the device page of hosts and storage systems.
* A virtual machine is identified by its ID on the platform within its cluster
  (unique there); a sync updates its host when it moved, so migrations never create a
  second record. Names may repeat, as they can on the platforms.
* Proxmox VE connections: the API token (read rights, PVEAuditor) is checked, then
  stored encrypted with the instance keys and never shown again. The user chooses
  nodes, machines and storage to take over; nodes not yet documented become server
  devices in a chosen room with a chosen device type. Optionally new machines are
  taken over automatically; machines that were offered and left out stay out.
  Connections update by hand or every 1, 6 or 24 hours (a scheduled task); a
  connection set up by someone who can no longer sign in stops with an error.
  Machines that disappeared from the platform are reported, not deleted.
* One-off imports from VMware vCenter (REST API), Synology DSM (volumes, pools,
  shares) and QNAP QTS or QuTS hero (volumes, shared folders) take credentials that are used once and not stored.
* The address is the user's, so every platform request is https only, resolved
  once with loopback, link local (cloud metadata), multicast and unspecified
  addresses refused, sent to the checked address, without redirects, with a deadline
  per import and limits on answer size and request count. Certificates are verified
  or pinned by their SHA-256 fingerprint.

## Consequences

* Private networks are reachable on purpose (hypervisors live there); who may create
  connections is governed by the cluster permissions.
* Network cards keep their addresses as text; linking them to IPAM is a later step.
