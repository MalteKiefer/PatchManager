# User guide

## Choose a tenant

If you have access to several tenants (customers), pick one in the selector at
the top. Everything you see and create belongs to that tenant.

## Locations

**Locations** opens a map with the tenant's sites and a list next to it.
Click a marker or a name to open the site. The site page shows all buildings,
floors and rooms as a tree: one more click opens a room.

* **Site:** name, short code, address and map position. When creating a site,
  click the map to set latitude and longitude.
* **Building, floor, room:** open the parent and use **Add**. Names are unique
  within their parent. Floors carry a level (0 ground floor, negative for
  basements).
* Objects can only be deleted when they are empty.
* The breadcrumb at the top always shows where you are.

## Racks

Rooms list their racks with height, status and how many units are used. A rack
page shows the elevation for the **Front** or **Rear** face.

* **Reserve units** blocks units for planned equipment, on the front, the rear
  or both faces. Overlapping reservations are refused.
* Move a block by dragging it, or focus it with the Tab key and press the arrow
  keys.
* Utilisation shows used and free units per face.
* Settings: height (1 to 100 U), width, depth, unit numbering (from the bottom
  by default), status, serial number and asset tag.

## Device library

*Device library → Add from catalog* searches more than 12 000 device types of the
community device type library by manufacturer, model or part number and copies one
with all its ports into your library, shared or for the current tenant.

**Library** lists device types: shared ones and the custom types of the current
tenant. A type defines vendor, model, form (rack mounted or floor device),
category, height in units, whether it is full depth, a naming convention (for
example `SW-{n:02}` for SW-01, SW-02, ...) and its ports.

* **Add ports** creates a group: name pattern (`Gi1/0/{n}`), count, first
  number, kind, connector and speed. Front ports of patch panels map to rear
  ports; choose the rear group they map to.
* **Images:** upload front and rear photos in the files section, then select
  them; the rack elevation shows them.
* **Copy as custom type** copies a shared type into the current tenant to adapt it.
* **Import / export** uses YAML or JSON in the community device type library format.

## Devices

* **Mount device** on a rack page, or **Add device** on a room page for floor
  devices (access points, cameras, outlets, printers ...).
* Ports are created from the type. Open a port to label it, disable it, set its
  speed, MAC address and rear port mapping, or attach photos and notes.
* **Placement** moves a device to another rack position, another rack or out of
  the rack. Full depth devices always take both faces.

## Photos, files and notes

Sites, buildings, floors, rooms, racks, devices, ports and device types carry
photos, PDFs and Markdown notes. Uploads are checked by content, re-encoded and
stored encrypted; only people who can see the object can open its files.

## Cabling

Open a port and choose **Connect**: pick the device at the other end and one of
its free ports, then kind (patch cord or permanent link), media, colour, length
and label. Patch panels pass the signal from each front port to its rear port,
so a typical path is switch port → patch cord → panel front → permanent link
between panel rears → panel front → patch cord → end device.

**Trace path** shows the whole path as a graph and as a list of full locations.
It is *complete* when it ends at active ports on both sides. A device page lists
what each cabled port finally connects to. **Cables** lists and filters all
cables of the tenant.

## Floor plans

Open a floor and choose **Floor plan**. Upload an image or a PDF (the first page
is used). Choose **Set scale**, click two points whose distance you know and
enter it in metres. Under **Room outlines** choose **Draw**, click the corners of
the room and finish. Racks and floor devices not yet on the plan are listed on
the right: choose one, then click where it stands. Drag symbols to move them, or
focus one and use the arrow keys (Shift for bigger steps). Dropping a floor
device into another room's outline moves it to that room. Double click a symbol
to open it.

Lines join each placed floor device with the rack or device its cables lead to;
dashed lines are permanent links, the number says how many paths. Choose **Add
device here** and click inside a room outline to create a device at that point.
Floor devices appear as pins whose tip marks the exact spot in the room, so others
can find an outlet or access point later. Drag a pin to move it. Click a pin (or focus
it and press Enter) to see the device, its room, its photos and notes and where each
of its ports is patched; Esc closes the details.
**Draw new room** works like drawing an outline: click the corners, choose **Finish
outline**, then name the new room on the next page.

## Rooms

A room page shows its racks, a comparison of **planned and installed** devices per
category, the surveys of the room and every device outside the racks with each port
and where it is patched (device, port and rack). The planned numbers come from the
room type; **Change plan** sets numbers for this room alone (leave a field empty to
use the room type's number, enter 0 to plan none).

**New survey** records a walk through the room: date, who, status, notes and how
many devices of each category were found. The survey page lists planned,
documented and found side by side and marks differences. Add photos and notes to a
survey like to any other object.

## Search

Use the search field in the header. Search finds names, codes, serial numbers,
asset tags, port labels and MAC addresses, cable labels, VLAN names and IDs, WLAN
names and text in notes, also with small typos. Filter by type on the right and
save a search under a name to run it again.

## History

**Activity** lists recent changes in the tenant; filter by type, action, name,
person and date. Every object page links to its **History**. Open a change to see
the values before and after; **Restore earlier values** sets changed fields back
if nobody changed them again since. Moves (room, rack) are not restored.

## Import and export

**Import/Export** imports locations, racks, devices, VLANs and cables from CSV or
XLSX. Download a template, fill it in and choose **Check file**: nothing changes
yet, every line shows what would happen. **Import now** applies the file, or
nothing if a line fails. Objects with the same name are updated; empty cells keep
their value. Exports use the same columns, so you can export, edit and import
again. PDF reports: site report, patch list and bill of materials per site, the
rack elevation on rack pages and the port list on device pages.

## Device configurations

A device page shows its latest stored configuration; *Versions* lists all of them.
Store a new version by pasting the text or choosing a file; an unchanged paste does
not create a version. Open a version to read it with line numbers, download it, or
compare two versions. Configurations are encrypted at rest and every view is
recorded in the audit log. Backup tools can store versions through the API:

```bash
curl -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "$(jq -Rs '{content: ., comment: "nightly"}' sw1.cfg)" \
  https://patchmanager.example.com/api/v1/devices/<device id>/configs/
```

## IP addresses

*IP addresses* keeps prefixes and addresses, optionally in VRFs (separate routing
tables; without a VRF everything is in the global table).

* **Prefixes** show as a tree with how much of each is in use. A *container* holds
  other prefixes and counts the space they take; other prefixes count addresses.
* A prefix page lists its child prefixes, its addresses and the free ranges, and
  offers the next free address with one click.
* Put an address on a port from the port's page (*Add IP address*); it then shows
  on the device page next to the port and follows the device when it moves.
* Prefixes and addresses can be imported and exported like VLANs (*Import and
  export*), and the search finds them by address or DNS name.

## Switching and routing

**Switching and routing** on a device page shows its ports coloured by untagged
VLAN with marks for tagged VLANs, edge ports, guards and tunnelled ports, then its
VLANs with addresses, static routes, BGP neighbors, OSPF, spanning tree, link
aggregations, device profiles and the stack it belongs to.

**Read a configuration** takes a configuration of ArubaOS-Switch (or an Aruba
Central template with its variables as JSON), AOS-CX, Cisco IOS, IOS-XE or NX-OS,
FortiOS, Junos, RouterOS, HPE Comware, H3C, Huawei VRP or LANCOM LCOS (a script in
English); the platform is recognised or can be chosen: paste it, choose a file or a stored version.
**Show changes** lists what would change and what is left out; **Apply changes**
saves it. Passwords, keys and SNMP communities are never read. **As ArubaOS-Switch
configuration** writes the documentation back as configuration text.

Port settings such as link aggregation, MTU, edge port, guards and OSPF can also be
edited on each port. Under *Networks → Stacks* you group switches into stacks, VSF,
VSX or MLAG pairs or a virtual chassis, with member number, role and priority.

## Customers, systems and lines

*Customers and systems* lists organisations (customers, suppliers, providers,
service partners, departments) with their contacts, systems such as a phone system
or building automation, and lines from providers with their contracts. On the page
of a device, building, VLAN, network, address, configuration or stack, **Link** puts
it into a system or assigns it to an organisation, for example as its customer. A
system's page lists everything linked to it.

Devices also record their supplier, warranty, support contract and end of life.

## DHCP and DNS

Under *IP networks*, **DHCP** documents scopes (range, gateway, DNS servers, domain,
lease time, relays and further options) with reservations by MAC address, and
**DNS** documents zones with their records. A scope downloads as a Kea
configuration, a zone as a zone file.

## VLANs and WLANs

**Networks** holds VLANs (for the whole tenant or one site) and WLANs. On a
network port set the mode: *access* with one VLAN, or *trunk* with an optional
native VLAN and tagged VLANs. A WLAN can bridge into a VLAN; add the wireless
ports of access points that broadcast it.

## Your data

*Profile → Download my data* gives you a JSON file with everything PatchManager
stores about you: profile, roles, sessions, second factors (without secrets), API
tokens, sign in links and your actions in the audit log. To have your account
anonymised, ask an administrator.
