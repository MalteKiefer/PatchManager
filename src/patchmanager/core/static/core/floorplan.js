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
    layer.append(el("polygon", { points: room.points.map((p) => p.join(",")).join(" "), class: "fill-brand-500/10 stroke-brand-600", "stroke-width": 2 }));
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
    g.append(el("circle", { r: item.r, class: "fill-amber-500 stroke-white", "stroke-width": 1.5 }));
    g.append(el("text", { y: 4, "text-anchor": "middle", class: "fill-white text-[11px] font-bold" }, item.glyph));
    g.append(el("text", { y: item.r + 13, "text-anchor": "middle", class: "fill-slate-900 text-[11px] dark:fill-white" }, item.name));
  }
  g.addEventListener("dblclick", () => location.assign(item.url));
  return g;
}

function drawItems() {
  const layer = document.getElementById("layer-items");
  layer.replaceChildren();
  for (const rack of scene.racks) if (rack.x !== null) layer.append(itemNode("rack", rack));
  for (const device of scene.devices) if (device.x !== null) layer.append(itemNode("device", device));
}

function drawDraft() {
  const layer = document.getElementById("layer-draft");
  layer.replaceChildren();
  if (!draft.length) return;
  const shape = mode === "outline" ? "polygon" : "polyline";
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

function setMode(next) {
  mode = next;
  draft = [];
  pending = null;
  for (const button of document.querySelectorAll("[data-mode]")) button.setAttribute("aria-pressed", String(button.dataset.mode === mode));
  document.getElementById("scale-form")?.classList.toggle("hidden", mode !== "scale");
  document.getElementById("scale-form")?.classList.toggle("flex", mode === "scale");
  document.getElementById("outline-bar")?.classList.toggle("hidden", mode !== "outline");
  document.getElementById("outline-bar")?.classList.toggle("flex", mode === "outline");
  hint.textContent = text[mode];
  drawDraft();
}

if (svg) {
  drawRooms();
  drawItems();

  let drag = null;
  svg.addEventListener("pointerdown", (event) => {
    const group = event.target.closest?.(".plan-item[tabindex]");
    if (mode !== "move" || !group) return;
    drag = { group, start: toPlan(event), kind: group.dataset.kind, id: group.dataset.id };
    svg.setPointerCapture(event.pointerId);
  });
  svg.addEventListener("pointermove", (event) => {
    if (!drag) return;
    const [x, y] = toPlan(event);
    drag.group.setAttribute("transform", `translate(${x} ${y})`);
  });
  svg.addEventListener("pointerup", (event) => {
    if (!drag) return;
    const point = toPlan(event);
    const moved = Math.hypot(point[0] - drag.start[0], point[1] - drag.start[1]) > 2;
    const { kind, id } = drag;
    drag = null;
    if (moved) moveItem(kind, id, point);
  });

  svg.addEventListener("click", (event) => {
    const point = toPlan(event);
    if (mode === "place" && pending) {
      moveItem(pending.kind, pending.id, point).then(() => location.reload());
      setMode("move");
    } else if (mode === "scale") {
      draft = draft.length >= 2 ? [point] : [...draft, point];
      drawDraft();
      if (draft.length === 2) document.getElementById("scale-metres").focus();
    } else if (mode === "outline") {
      draft.push(point);
      drawDraft();
    }
  });

  svg.addEventListener("keydown", (event) => {
    const group = event.target.closest?.(".plan-item[tabindex]");
    const steps = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[event.key];
    if (!group || !steps) return;
    event.preventDefault();
    const list = group.dataset.kind === "rack" ? scene.racks : scene.devices;
    const item = list.find((i) => i.id === group.dataset.id);
    const step = event.shiftKey ? 50 : 10;
    moveItem(group.dataset.kind, item.id, [item.x + steps[0] * step, item.y + steps[1] * step]);
  });

  for (const button of document.querySelectorAll("[data-mode]")) button.addEventListener("click", () => setMode(button.dataset.mode));
  for (const button of document.querySelectorAll("[data-place]")) {
    button.addEventListener("click", () => { setMode("place"); pending = { kind: button.dataset.place, id: button.dataset.id }; });
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
