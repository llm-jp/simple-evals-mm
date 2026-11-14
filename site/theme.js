// Theme toggle (every page): each click shows the other theme; when that equals the system setting the page follows
// the system again. The saved theme is applied before first paint by an inline script in each page's <head>.
(function () {
  const btn = document.getElementById("theme-toggle");
  if (!btn) return;
  const sys = () => matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  function show() {
    const root = document.documentElement, shown = root.dataset.theme || sys();
    root.dataset.themeShown = shown;
    const label = `Theme: ${shown}${root.dataset.theme ? "" : " (system)"} (click for ${shown === "dark" ? "light" : "dark"})`;
    btn.title = label; btn.setAttribute("aria-label", label);
  }
  btn.addEventListener("click", () => {
    const root = document.documentElement, next = (root.dataset.theme || sys()) === "dark" ? "light" : "dark";
    if (next === sys()) delete root.dataset.theme; else root.dataset.theme = next;
    try { root.dataset.theme ? localStorage.setItem("theme", root.dataset.theme) : localStorage.removeItem("theme"); } catch (e) {}
    show();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", show);
  show();
})();
