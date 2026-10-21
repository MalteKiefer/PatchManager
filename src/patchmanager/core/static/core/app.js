// Site behaviour. Loaded as a module before Alpine (CSP build) so components
// are registered on alpine:init. No inline scripts anywhere (strict CSP).

const THEMES = ["system", "light", "dark"];

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
    get label() { return { system: "Theme: system", light: "Theme: light", dark: "Theme: dark" }[this.theme]; },
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
