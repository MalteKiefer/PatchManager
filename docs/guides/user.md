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

Devices, ports and cabling follow in the next releases.

## Device library

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

## VLANs and WLANs

**Networks** holds VLANs (for the whole tenant or one site) and WLANs. On a
network port set the mode: *access* with one VLAN, or *trunk* with an optional
native VLAN and tagged VLANs. A WLAN can bridge into a VLAN; add the wireless
ports of access points that broadcast it.
