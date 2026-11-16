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
// Phones: the sticky header slides away while scrolling down and comes back on scrolling up (and near the top), so
// it does not cover the page; a keyboard focus inside it brings it back.
(function () {
  const h = document.querySelector("header.site");
  if (!h) return;
  const narrow = matchMedia("(max-width: 760px)");
  let lastY = scrollY, ticking = false;
  addEventListener("scroll", () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      const y = scrollY, d = y - lastY;
      if (!narrow.matches || y < 64 || h.classList.contains("menu-open")) h.classList.remove("hdr-hidden");
      else if (d > 6) h.classList.add("hdr-hidden");
      else if (d < -6) h.classList.remove("hdr-hidden");
      if (Math.abs(d) > 6 || y < 64) lastY = y;
      ticking = false;
    });
  }, {passive: true});
  h.addEventListener("focusin", () => h.classList.remove("hdr-hidden"));
  narrow.addEventListener("change", () => h.classList.remove("hdr-hidden"));
})();
// Phones: the ☰ button opens the links (and the theme toggle) in a panel under the header; a link, a tap outside or
// Escape closes it.
(function () {
  const h = document.querySelector("header.site"), btn = document.getElementById("menu-btn");
  if (!h || !btn) return;
  const set = open => { h.classList.toggle("menu-open", open); btn.setAttribute("aria-expanded", String(open)); btn.setAttribute("aria-label", open ? "Close menu" : "Menu"); };
  btn.addEventListener("click", e => { e.stopPropagation(); set(!h.classList.contains("menu-open")); });
  h.querySelectorAll("nav.links a").forEach(a => a.addEventListener("click", () => set(false)));
  document.addEventListener("click", e => { if (!h.contains(e.target)) set(false); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && h.classList.contains("menu-open")) { set(false); btn.focus(); } });
  matchMedia("(max-width: 600px)").addEventListener("change", () => set(false));
})();
