// Floor plan editor. All coordinates are pixels of the plan image (the SVG viewBox).
// Data comes from <script id="plan-scene" type="application/json">; changes are POSTed as JSON.

const SVG = "http://www.w3.org/2000/svg";
const svg = document.getElementById("plan");
const scene = JSON.parse(document.getElementById("plan-scene").textContent);
const status = document.getElementById("plan-status");
const hint = document.getElementById("plan-hint");
const text = document.getElementById("plan-text").dataset; // translated strings
let mode = "move";
let pending = null; // item waiting to be placed
let draft = []; // clicked points (scale or outline)
let outlineRoom = null;
let selected = null; // pin whose details are in the side panel
const panel = document.getElementById("plan-panel");
const emptyPanel = panel ? [...panel.childNodes] : [];

const csrf = () => document.querySelector("meta[name=csrf-token]").content;
const say = (text) => { if (status) status.textContent = text || ""; };

async function post(action, body) {
  const response = await fetch(`${svg.dataset.base}${action}/`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
    credentials: "same-origin",
    body: JSON.stringify(body),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || text.failed);
  return data;
}

function el(name, attrs = {}, text) {
  const node = document.createElementNS(SVG, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text !== undefined) node.textContent = text; // always text, never markup
  return node;
}

function toPlan(event) {
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  const p = point.matrixTransform(svg.getScreenCTM().inverse());
  return [Math.max(0, Math.min(Number(svg.dataset.width), p.x)), Math.max(0, Math.min(Number(svg.dataset.height), p.y))];
}

function drawRooms() {
  const layer = document.getElementById("layer-rooms");
  layer.replaceChildren();
  for (const room of scene.rooms) {
    if (!room.points.length) continue;
    // While placing, the item's own room stands out as the target.
    const target = pending && pending.room === room.id;
    layer.append(el("polygon", { points: room.points.map((p) => p.join(",")).join(" "),
                                 class: target ? "plan-room is-target" : "plan-room fill-brand-500/10 stroke-brand-600", "stroke-width": 2 }));
    const cx = room.points.reduce((s, p) => s + p[0], 0) / room.points.length;
    const cy = room.points.reduce((s, p) => s + p[1], 0) / room.points.length;
    layer.append(el("text", { x: cx, y: cy, "text-anchor": "middle", class: "fill-brand-700 text-[14px] font-semibold" }, room.name));
  }
}

function itemNode(kind, item) {
  const g = el("g", { class: "plan-item", "data-kind": kind, "data-id": item.id, transform: `translate(${item.x} ${item.y})`,
                      "aria-label": item.name });
  if (item.can_change) { g.setAttribute("tabindex", "0"); g.setAttribute("role", "button"); g.classList.add("cursor-move"); }
  if (kind === "rack") {
    g.append(el("rect", { x: -item.w / 2, y: -item.h / 2, width: item.w, height: item.h, rx: 2, transform: `rotate(${item.rotation})`,
                          class: "fill-slate-700 stroke-white", "stroke-width": 1.5 }));
    g.append(el("text", { y: item.h / 2 + 14, "text-anchor": "middle", class: "fill-slate-900 text-[12px] font-semibold dark:fill-white" }, item.name));
  } else {
    // A pin whose tip marks the exact spot; the head carries the category sign.
    const r = Math.max(item.r, 9);
    const head = -r * 1.9;
    g.append(el("ellipse", { cy: 0, rx: r * 0.45, ry: r * 0.18, class: "plan-pin-shadow" }));
    g.append(el("path", {
      d: `M0 0 C ${-r * 0.35} ${-r * 0.8} ${-r} ${head + r * 0.75} ${-r} ${head} A ${r} ${r} 0 1 1 ${r} ${head} C ${r} ${head + r * 0.75} ${r * 0.35} ${-r * 0.8} 0 0 Z`,
      fill: item.colour || "#f59e0b", class: "plan-pin stroke-white", "stroke-width": 1.5,
    }));
    g.append(el("text", { y: head + 4, "text-anchor": "middle", class: "fill-white text-[11px] font-bold" }, item.glyph));
    g.append(el("text", { y: 14, "text-anchor": "middle", class: "fill-slate-900 text-[11px] dark:fill-white" }, item.name));
  }
  if (!item.can_change) { g.setAttribute("tabindex", "0"); g.setAttribute("role", "button"); }
  if (selected && selected.kind === kind && selected.id === item.id) g.classList.add("is-selected");
  g.addEventListener("dblclick", () => location.assign(item.url));
  return g;
}

function position(kind, id) {
  const item = (kind === "rack" ? scene.racks : scene.devices).find((i) => i.id === id);
  return item && item.x !== null ? [item.x, item.y] : null;
}

// Cabling between pins: one line per pair, solid for patch cables, dashed for permanent links.
function drawLinks() {
  const layer = document.getElementById("layer-links");
  layer.replaceChildren();
  for (const link of scene.links || []) {
    const a = position(...link.from);
    const b = position(...link.to);
    if (!a || !b) continue;
    const attrs = { x1: a[0], y1: a[1], x2: b[0], y2: b[1], stroke: link.colour, "stroke-width": 3, class: "plan-link", "stroke-linecap": "round" };
    if (link.dashed) attrs["stroke-dasharray"] = "8 6";
    layer.append(el("line", attrs));
    if (link.count > 1) {
      layer.append(el("text", { x: (a[0] + b[0]) / 2, y: (a[1] + b[1]) / 2 - 6, "text-anchor": "middle", class: "plan-link-count" }, `${link.count}`));
    }
  }
}

function roomAt([x, y]) {
  // Ray casting, as on the server (geometry.contains).
  return scene.rooms.find((room) => {
    const pts = room.points;
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const [xi, yi] = pts[i];
      const [xj, yj] = pts[j];
      if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
    }
    return pts.length >= 3 && inside;
  });
}

async function select(kind, id) {
  selected = kind ? { kind, id } : null;
  for (const node of svg.querySelectorAll(".plan-item")) {
    node.classList.toggle("is-selected", Boolean(selected) && node.dataset.kind === kind && node.dataset.id === id);
  }
  if (!panel) return;
  if (!selected) { panel.replaceChildren(...emptyPanel); return; }
  const item = (kind === "rack" ? scene.racks : scene.devices).find((i) => i.id === id);
  panel.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(item.panel, { credentials: "same-origin" });
    if (!response.ok) throw new Error(String(response.status));
    panel.innerHTML = await response.text(); // server rendered, escaped template
    for (const swatch of panel.querySelectorAll("[data-swatch]")) swatch.style.backgroundColor = swatch.dataset.swatch;
    panel.querySelector("[data-unplace]")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      try { await post("unplace", { kind: button.dataset.unplace, id: button.dataset.id }); location.reload(); } catch (e) { say(e.message); }
    });
  } catch {
    panel.textContent = text.failed;
  } finally {
    panel.removeAttribute("aria-busy");
  }
}

function drawItems() {
  const layer = document.getElementById("layer-items");
  layer.replaceChildren();
  for (const rack of scene.racks) if (rack.x !== null) layer.append(itemNode("rack", rack));
  for (const device of scene.devices) if (device.x !== null) layer.append(itemNode("device", device));
  drawLinks();
}

function drawDraft() {
  const layer = document.getElementById("layer-draft");
  layer.replaceChildren();
  if (!draft.length) return;
  const shape = mode === "outline" || mode === "room" ? "polygon" : "polyline";
  layer.append(el(shape, { points: draft.map((p) => p.join(",")).join(" "), class: "fill-amber-400/20 stroke-amber-500", "stroke-width": 2, "stroke-dasharray": "6 4" }));
  for (const [x, y] of draft) layer.append(el("circle", { cx: x, cy: y, r: 5, class: "fill-amber-500" }));
}

async function moveItem(kind, id, point) {
  try {
    const result = await post("place", { kind, id, point });
    const item = (kind === "rack" ? scene.racks : scene.devices).find((i) => i.id === id);
    item.x = result.x; item.y = result.y;
    if (result.moved) location.reload();
    else say(result.notice);
    drawItems();
  } catch (error) {
    say(error.message);
    drawItems();
  }
}

function setMode(next, item = null) {
  mode = next;
  draft = [];
  pending = item;
  svg.dataset.planMode = mode;
  svg.closest(".plan-frame")?.classList.toggle("is-placing", mode === "place");
  hint.classList.toggle("is-placing", mode === "place");
  for (const button of document.querySelectorAll("[data-place]")) {
    button.setAttribute("aria-pressed", String(Boolean(item) && button.dataset.id === item.id && button.dataset.place === item.kind));
  }
  for (const button of document.querySelectorAll("[data-mode]")) button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
  document.getElementById("scale-form")?.classList.toggle("hidden", mode !== "scale");
  document.getElementById("scale-form")?.classList.toggle("flex", mode === "scale");
  const outlining = mode === "outline" || mode === "room";
  document.getElementById("outline-bar")?.classList.toggle("hidden", !outlining);
  document.getElementById("outline-bar")?.classList.toggle("flex", outlining);
  if (mode === "room") document.getElementById("outline-label").textContent = text.newRoomLabel;
  hint.textContent = item ? text.placing.replace("%(name)s", item.name) : text[mode];
  drawDraft();
  drawRooms();
}

if (svg) {
  drawRooms();
  drawItems();

  let drag = null;
  svg.addEventListener("pointerdown", (event) => {
    const group = event.target.closest?.(".plan-item[tabindex]");
    if (mode !== "move" || !group) return;
    if (!group.classList.contains("cursor-move")) { select(group.dataset.kind, group.dataset.id); return; } // read only
    drag = { group, start: toPlan(event), kind: group.dataset.kind, id: group.dataset.id };
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const [x, y] = toPlan(event);
    drag.group.setAttribute("transform", `translate(${x} ${y})`);
    const item = (drag.kind === "rack" ? scene.racks : scene.devices).find((i) => i.id === drag.id);
    if (item) { item.x = x; item.y = y; drawLinks(); }
  });
  svg.addEventListener("pointerup", (event) => {
    if (!drag) return;
    const point = toPlan(event);
    const moved = Math.hypot(point[0] - drag.start[0], point[1] - drag.start[1]) > 2;
    const { kind, id } = drag;
    drag = null;
    if (moved) moveItem(kind, id, point);
    else select(kind, id);
  });

  svg.addEventListener("click", (event) => {
    const point = toPlan(event);
    if (mode === "add") {
      const room = roomAt(point);
      if (!room) { say(text.outside); return; }
      const query = new URLSearchParams({ room: room.id, plan_x: point[0].toFixed(1), plan_y: point[1].toFixed(1) });
      location.assign(`${text.newDevice}?${query}`);
    } else if (mode === "place" && pending) {
      moveItem(pending.kind, pending.id, point).then(() => location.reload());
      setMode("move");
    } else if (mode === "scale") {
      draft = draft.length >= 2 ? [point] : [...draft, point];
      drawDraft();
      if (draft.length === 2) document.getElementById("scale-metres").focus();
    } else if (mode === "outline" || mode === "room") {
      draft.push(point);
      drawDraft();
    }
  });

  svg.addEventListener("keydown", (event) => {
    const group = event.target.closest?.(".plan-item[tabindex]");
    if (group && (event.key === "Enter" || event.key === " ")) {
      event.preventDefault();
      select(group.dataset.kind, group.dataset.id);
      return;
    }
    if (event.key === "Escape") { select(null); return; }
    if (!group?.classList.contains("cursor-move")) return;
    const steps = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
    if (!group || !steps) return;
    event.preventDefault();
    const list = group.dataset.kind === "rack" ? scene.racks : scene.devices;
    const item = list.find((i) => i.id === group.dataset.id);
    const step = event.shiftKey ? 50 : 10;
    moveItem(group.dataset.kind, item.id, [item.x + steps[0] * step, item.y + steps[1] * step]);
  });

  for (const button of document.querySelectorAll("[data-mode]")) button.addEventListener("click", () => setMode(button.dataset.mode));
  // Esc leaves placing, drawing or measuring from anywhere on the page.
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && mode !== "move") setMode("move");
  });
  for (const button of document.querySelectorAll("[data-place]")) {
    button.addEventListener("click", () => {
      setMode("place", { kind: button.dataset.place, id: button.dataset.id, name: button.dataset.name, room: button.dataset.room });
    });
  }
  for (const button of document.querySelectorAll("[data-outline]")) {
    button.addEventListener("click", () => {
      setMode("outline");
      outlineRoom = button.dataset.outline;
      document.getElementById("outline-label").textContent = button.dataset.name;
    });
  }
  for (const button of document.querySelectorAll("[data-clear-outline]")) {
    button.addEventListener("click", async () => {
      try { await post("outline", { room: button.dataset.clearOutline, points: [] }); location.reload(); } catch (e) { say(e.message); }
    });
  }
  document.getElementById("outline-save")?.addEventListener("click", async () => {
    if (mode === "room") {
      // The room is created on its own page; the outline travels along and is checked there.
      location.assign(`${text.newRoom}?${new URLSearchParams({ outline: JSON.stringify(draft) })}`);
      return;
    }
    try { await post("outline", { room: outlineRoom, points: draft }); location.reload(); } catch (e) { say(e.message); }
  });
  document.getElementById("outline-cancel")?.addEventListener("click", () => setMode("move"));
  document.getElementById("scale-save")?.addEventListener("click", async () => {
    if (draft.length !== 2) { say(text.twoPoints); return; }
    try {
      await post("scale", { a: draft[0], b: draft[1], metres: document.getElementById("scale-metres").value });
      location.reload();
    } catch (e) { say(e.message); }
  });
  svg.dataset.ready = "1";
}
