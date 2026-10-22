// Site behaviour. Loaded as a module before Alpine (CSP build) so components
// are registered on alpine:init. No inline scripts anywhere (strict CSP).

const THEMES = ["system", "light", "dark"];
// Translated texts from <div id="js-text" data-...> in the base layout.
const text = (key, fallback = "") => document.getElementById("js-text")?.dataset[key] || fallback;

function applyTheme(theme) {
  const dark = theme === "dark" || (theme === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.dataset.theme = theme;
}

document.addEventListener("alpine:init", () => {
  window.Alpine.data("dropdown", () => ({
    open: false,
    toggle() { this.open = !this.open; },
    close() { this.open = false; },
  }));
  window.Alpine.data("themeToggle", () => ({
    theme: document.documentElement.dataset.theme || "system",
    next() {
      this.theme = THEMES[(THEMES.indexOf(this.theme) + 1) % THEMES.length];
      document.cookie = `pm_theme=${this.theme}; path=/; max-age=31536000; samesite=lax${location.protocol === "https:" ? "; secure" : ""}`;
      applyTheme(this.theme);
    },
    get label() {
      const key = `theme${this.theme[0].toUpperCase()}${this.theme.slice(1)}`;
      return text(key, `Theme: ${this.theme}`);
    },
  }));
  window.Alpine.data("menu", () => ({
    open: false,
    toggle() { this.open = !this.open; },
    get label() { return text(this.open ? "menuClose" : "menuOpen", "Menu"); },
  }));
});

applyTheme(document.documentElement.dataset.theme || "system");
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(document.documentElement.dataset.theme));

// Confirmation for destructive forms: <form data-confirm="Really?">
document.addEventListener("submit", (event) => {
  const message = event.target.dataset?.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

// Auto submit selects: <select data-autosubmit>
document.addEventListener("change", (event) => {
  if (event.target.matches?.("select[data-autosubmit]")) event.target.form.requestSubmit();
});

// Colour swatches: <span data-swatch="#rrggbb"> (set via CSSOM, allowed by the CSP).
for (const swatch of document.querySelectorAll("[data-swatch]")) {
  if (/^#[0-9a-f]{6}$/i.test(swatch.dataset.swatch)) swatch.style.backgroundColor = swatch.dataset.swatch;
}

// Ctrl+K or Cmd+K (or "/" outside fields) jumps to the search box.
document.addEventListener("keydown", (event) => {
  const field = event.target.closest?.("input, textarea, select, [contenteditable]");
  const combo = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k";
  if (combo || (event.key === "/" && !field)) {
    const search = document.getElementById("global-search");
    if (!search) return;
    event.preventDefault();
    search.focus();
    search.select();
  }
});

// Regions that scroll sideways (wide tables) must be reachable with the keyboard.
for (const region of document.querySelectorAll(".overflow-x-auto, .overflow-auto, .rack-scroll")) {
  if (region.scrollWidth > region.clientWidth || region.scrollHeight > region.clientHeight) {
    if (!region.hasAttribute("tabindex")) region.tabIndex = 0;
  }
}

// Form fields that only apply for one value of another field, for example
// data-show-if="form=rack": hidden (with their label) while it has another value.
function showIf() {
  for (const input of document.querySelectorAll("[data-show-if]")) {
    const [name, value] = input.dataset.showIf.split("=");
    const source = input.form?.elements.namedItem(name);
    if (!source) continue;
    const update = () => { input.closest(".field").hidden = source.value !== value; };
    source.addEventListener("change", update);
    update();
  }
}
showIf();
