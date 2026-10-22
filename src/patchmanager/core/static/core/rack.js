// Rack elevation: drag blocks to other units, or move them with the arrow keys.
// Moves are POSTed as JSON; the server validates (overlap, bounds, permission).

// Translated texts from <div id="js-text" data-...> in the base layout.
const text = (key, fallback = "") => document.getElementById("js-text")?.dataset[key] || fallback;

const svg = document.getElementById("rack-elevation");
const status = document.getElementById("rack-status");

function csrf() {
  return document.querySelector("meta[name=csrf-token]").content;
}

async function move(block, start) {
  const response = await fetch(block.dataset.moveUrl, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
    credentials: "same-origin",
    body: JSON.stringify({ start }),
  });
  if (response.ok) {
    location.reload();
    return;
  }
  const data = await response.json().catch(() => ({}));
  status.textContent = data.error || text("moveFailed", "The block could not be moved.");
  block.removeAttribute("transform");
}

if (svg) {
  const unitPx = Number(svg.dataset.unitPx);
  const units = Number(svg.dataset.units);
  const descending = svg.dataset.descending === "1";
  // Moving the drawing down by one unit changes the unit number by this much.
  const downStep = descending ? 1 : -1;

  const clamp = (start, size) => Math.min(Math.max(start, 1), units - size + 1);

  for (const block of svg.querySelectorAll(".rack-block[data-move-url]")) {
    const start = Number(block.dataset.start);
    const size = Number(block.dataset.size);

    block.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      event.preventDefault();
      const delta = event.key === "ArrowDown" ? downStep : -downStep;
      const target = clamp(start + delta, size);
      if (target !== start) move(block, target);
    });

    block.addEventListener("pointerdown", (event) => {
      if (event.target.closest("a")) return; // ports are links, not drag handles
      block.setPointerCapture(event.pointerId);
      const scale = svg.viewBox.baseVal.height / svg.getBoundingClientRect().height;
      const originY = event.clientY;
      const stepsAt = (e) => Math.round(((e.clientY - originY) * scale) / unitPx);
      const onMove = (e) => {
        block.setAttribute("transform", `translate(0 ${stepsAt(e) * unitPx})`);
      };
      const onUp = (e) => {
        block.removeEventListener("pointermove", onMove);
        block.removeEventListener("pointerup", onUp);
        const target = clamp(start + stepsAt(e) * downStep, size);
        if (target !== start) move(block, target);
        else block.removeAttribute("transform");
      };
      block.addEventListener("pointermove", onMove);
      block.addEventListener("pointerup", onUp);
    });
  }
  svg.dataset.ready = "1"; // lets tests wait until blocks are interactive
}

// Double click (or Enter) on a device block opens the device.
if (svg) {
  for (const block of svg.querySelectorAll(".rack-block[data-href]")) {
    block.addEventListener("dblclick", () => location.assign(block.dataset.href));
    block.addEventListener("keydown", (event) => {
      if (event.key === "Enter") location.assign(block.dataset.href);
    });
  }
}

// --- Path highlight and selection -----------------------------------------
// Pointing at a port previews its path; a click selects it (or a device), keeps the
// path marked and loads the details into the side panel. Double click opens the page.
const drawing = document.querySelector(".rack-svg");
const panel = document.getElementById("rack-panel");
let selected = null;

function mark(id) {
  for (const el of document.querySelectorAll("[data-path].is-hot")) el.classList.remove("is-hot");
  drawing.classList.toggle("is-tracing", Boolean(id));
  if (!id) return;
  for (const el of document.querySelectorAll(`[data-path="${CSS.escape(id)}"]`)) {
    el.classList.add("is-hot");
  }
}

async function showPanel(url) {
  if (!panel || !url) return;
  panel.setAttribute("aria-busy", "true");
  try {
    const response = await fetch(url, { credentials: "same-origin" });
    if (!response.ok) throw new Error(String(response.status));
    panel.innerHTML = await response.text(); // server rendered, escaped template
    for (const swatch of panel.querySelectorAll("[data-swatch]")) {
      swatch.style.backgroundColor = swatch.dataset.swatch;
    }
  } catch {
    panel.textContent = text("failed", "The request failed.");
  } finally {
    panel.removeAttribute("aria-busy");
  }
}

function select(el) {
  if (selected) selected.classList.remove("is-selected");
  selected = el;
  if (!el) {
    mark("");
    return;
  }
  el.classList.add("is-selected");
  mark(el.dataset.path || "");
  showPanel(el.dataset.panel);
}

if (drawing) {
  for (const el of document.querySelectorAll("[data-path]")) {
    el.addEventListener("pointerenter", () => mark(el.dataset.path));
    el.addEventListener("pointerleave", () => mark(selected?.dataset.path || ""));
  }
  drawing.addEventListener("click", (event) => {
    if (drawing.dataset.panned === "1") return; // the click that ends a pan
    const port = event.target.closest(".rack-port");
    const block = event.target.closest(".rack-block[data-panel]");
    if (port) {
      event.preventDefault();
      select(port === selected ? null : port);
    } else if (block) {
      select(block === selected ? null : block);
    } else {
      select(null);
    }
  });
  drawing.addEventListener("dblclick", (event) => {
    const port = event.target.closest(".rack-port");
    if (port) location.assign(port.getAttribute("href"));
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") select(null);
  });
}

// --- Zoom and pan --------------------------------------------------------
// The drawing keeps its proportions; zooming sets its width. "Width" fits the
// width of the view, "All" shows everything. Ctrl or Cmd and the wheel zoom at the
// pointer; dragging an empty area scrolls. The choice is remembered per browser.
const stage = document.querySelector(".rack-scroll");
const zoomBar = document.getElementById("rack-zoom");
const level = document.getElementById("rack-zoom-level");
if (drawing && stage && zoomBar) {
  const box = drawing.viewBox.baseVal;
  let scale = 1;
  let mode = "width"; // "width", "height" or "free"
  const limits = () => [Math.min(fitHeight(), 0.1), 4];
  const fitWidth = () => (stage.clientWidth - 2) / box.width;
  const fitHeight = () => Math.min(fitWidth(), (stage.clientHeight - 2) / box.height);

  function apply(next, anchor) {
    const [low, high] = limits();
    const old = scale;
    scale = Math.min(Math.max(next, low), high);
    const point = anchor || { x: stage.clientWidth / 2, y: stage.clientHeight / 2 };
    const focusX = (stage.scrollLeft + point.x) / old;
    const focusY = (stage.scrollTop + point.y) / old;
    drawing.style.width = `${Math.round(box.width * scale)}px`;
    drawing.style.height = `${Math.round(box.height * scale)}px`;
    stage.scrollLeft = focusX * scale - point.x;
    stage.scrollTop = focusY * scale - point.y;
    if (level) level.textContent = `${Math.round((scale / fitWidth()) * 100)} %`;
  }

  function fit(next) {
    mode = next;
    scale = next === "height" ? fitHeight() : fitWidth();
    apply(scale);
    stage.scrollTop = 0;
    stage.scrollLeft = 0;
    try {
      localStorage.setItem("pm-rack-zoom", next);
    } catch {
      // storage blocked: the choice lasts for this page only
    }
  }

  const zoom = (factor, anchor) => {
    mode = "free";
    apply(scale * factor, anchor);
  };

  zoomBar.addEventListener("click", (event) => {
    const action = event.target.closest("button")?.dataset.zoom;
    if (action === "in") zoom(1.25);
    else if (action === "out") zoom(0.8);
    else if (action) fit(action);
  });
  stage.addEventListener(
    "wheel",
    (event) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      const rect = stage.getBoundingClientRect();
      zoom(event.deltaY < 0 ? 1.12 : 1 / 1.12, {
        x: event.clientX - rect.left,
        y: event.clientY - rect.top,
      });
    },
    { passive: false },
  );
  stage.addEventListener("keydown", (event) => {
    if (event.key === "+" || event.key === "=") zoom(1.25);
    else if (event.key === "-") zoom(0.8);
    else if (event.key === "0") fit("width");
    else return;
    event.preventDefault();
  });

  // Drag an empty area to move the view (blocks and ports keep their own dragging).
  stage.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target.closest(".rack-block, .rack-port, a")) return;
    const start = { x: event.clientX, y: event.clientY, left: stage.scrollLeft, top: stage.scrollTop };
    let moved = false;
    stage.setPointerCapture(event.pointerId);
    const onMove = (e) => {
      const dx = e.clientX - start.x;
      const dy = e.clientY - start.y;
      if (!moved && Math.hypot(dx, dy) < 4) return;
      moved = true;
      stage.classList.add("is-panning");
      stage.scrollLeft = start.left - dx;
      stage.scrollTop = start.top - dy;
    };
    const onUp = () => {
      stage.removeEventListener("pointermove", onMove);
      stage.removeEventListener("pointerup", onUp);
      stage.classList.remove("is-panning");
      drawing.dataset.panned = moved ? "1" : "";
      setTimeout(() => {
        drawing.dataset.panned = "";
      }, 0);
    };
    stage.addEventListener("pointermove", onMove);
    stage.addEventListener("pointerup", onUp);
  });

  let saved = "width";
  try {
    saved = localStorage.getItem("pm-rack-zoom") || "width";
  } catch {
    saved = "width";
  }
  fit(saved === "height" ? "height" : "width");
  new ResizeObserver(() => {
    if (mode !== "free") fit(mode);
  }).observe(stage);
}
