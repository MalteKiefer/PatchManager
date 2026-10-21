// Leaflet map: markers from <script id="map-markers" type="application/json">.
// With data-pick on #map, a click fills the latitude/longitude inputs.

function init() {
  const element = document.getElementById("map");
  if (!element || !window.L) return;
  const L = window.L;
  const markers = JSON.parse(document.getElementById("map-markers")?.textContent || "[]");
  const map = L.map(element, { scrollWheelZoom: false });
  L.tileLayer(element.dataset.tiles, { maxZoom: 19, attribution: element.dataset.attribution }).addTo(map);

  const points = [];
  for (const m of markers) {
    const link = document.createElement("a");
    link.href = m.url;
    link.textContent = m.name; // text only: names are user input
    L.circleMarker([m.lat, m.lng], { radius: 8, weight: 2, color: "#0f766e", fillOpacity: 0.6 })
      .bindPopup(link)
      .addTo(map);
    points.push([m.lat, m.lng]);
  }
  if (points.length) map.fitBounds(points, { maxZoom: 15, padding: [24, 24] });
  else map.setView([51.16, 10.45], 5);

  if (element.dataset.pick) {
    const lat = document.getElementById("id_latitude");
    const lng = document.getElementById("id_longitude");
    let pin = null;
    map.on("click", (event) => {
      lat.value = event.latlng.lat.toFixed(6);
      lng.value = event.latlng.lng.toFixed(6);
      if (pin) pin.setLatLng(event.latlng);
      else pin = L.circleMarker(event.latlng, { radius: 8, color: "#0f766e" }).addTo(map);
    });
  }
}

// Leaflet is a deferred classic script; modules run after it.
init();
