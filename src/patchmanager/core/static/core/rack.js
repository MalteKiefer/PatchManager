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
