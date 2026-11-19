// Shareable views, used by the leaderboard, Trends and Analysis pages:
//  * section links: every h2 / h3 of <main> gets an id and a "#" that puts the section's URL in the address bar and
//    copies it (with the page's current view state);
//  * view state in the query string (?k=v&...), so a section anchor (#...) can follow it; links of the earlier
//    #k=v form are still read;
//  * two buttons under each chart / table (as on Epoch AI's ECI page): Share (X, Instagram, copy the link to that view
//    in its current state) and Download (PNG / SVG of a chart, CSV of the values); a saved image carries the chart's
//    title, the LLM-jp logo with "simple-evals-mm · CC BY 4.0" at the bottom left and the link and date at the right.
(function () {
  const BASE = new URL(".", document.currentScript.src).href;  // the site root (share.js lives there)
  const STATE_HASH = /^#[a-z]+=/;
  const slug = t => t.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  const today = () => new Date().toISOString().slice(0, 10);

  // ---- view state in the query string
  // "k=v&k=v" of the current view: the query string, or a legacy state hash (#k=v&...)
  function stateString() {
    if (location.search.length > 1) return location.search.slice(1);
    return STATE_HASH.test(location.hash) ? location.hash.slice(1) : "";
  }
  // write the state ("k=v" parts, values already encoded); keeps an anchor, drops a legacy state hash
  function writeState(kv) {
    const hash = STATE_HASH.test(location.hash) ? "" : location.hash;
    const url = location.pathname + (kv.length ? "?" + kv.join("&") : "") + hash;
    if (url !== location.pathname + location.search + location.hash) history.replaceState(null, "", url);
  }
  const linkTo = id => location.origin + location.pathname + location.search + "#" + id;

  function copy(text, btn) {
    const done = ok => { if (!btn) return; const t = btn.dataset.label || btn.textContent; btn.dataset.label = t;
      btn.textContent = ok ? "Link copied" : "Copy failed"; setTimeout(() => { btn.textContent = t; }, 1600); };
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text).then(() => done(true), () => done(false));
    else { const a = document.createElement("textarea"); a.value = text; document.body.appendChild(a); a.select();
      let ok = false; try { ok = document.execCommand("copy"); } catch (e) { /* not allowed */ } a.remove(); done(ok); }
  }

  // ---- section links
  function anchors(root) {
    const used = new Set([...document.querySelectorAll("[id]")].map(e => e.id));
    for (const h of (root || document).querySelectorAll("main h2, main h3")) {
      if (h.querySelector("a.anchor")) continue;
      if (!h.id) {  // the section's own id for its h2, else a slug of the heading
        const sec = h.closest("section[id], .tr-sec[id]");
        let id = sec && h === sec.querySelector("h2, h3") && !sec.dataset.anchored ? sec.id : slug(h.textContent);
        if (sec && id === sec.id) sec.dataset.anchored = "1";
        else { let n = 2, base = id; while (used.has(id)) id = `${base}-${n++}`; h.id = id; }
        used.add(id); h.dataset.anchor = id;
      }
      const id = h.dataset.anchor || h.id;
      const a = document.createElement("a");
      a.className = "anchor"; a.href = "#" + id; a.textContent = "#";
      a.setAttribute("aria-label", "Link to this section (copies the link)"); a.title = "Link to this section";
      a.onclick = e => { e.preventDefault(); history.replaceState(null, "", location.pathname + location.search + "#" + id);
        copy(linkTo(id), null); document.getElementById(id).scrollIntoView({ behavior: "smooth", block: "start" }); };
      h.appendChild(a);
    }
    if (location.hash && !STATE_HASH.test(location.hash)) {  // ids added after load: go to the anchor now
      const el = document.getElementById(decodeURIComponent(location.hash.slice(1)));
      if (el) setTimeout(() => el.scrollIntoView({ block: "start" }), 0);
    }
  }

  // ---- export
  function download(name, blob) {
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  const csvCell = v => { const s = v == null ? "" : String(v); return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s; };
  const toCSV = rows => rows.map(r => r.map(csvCell).join(",")).join("\n") + "\n";
  // the visible text of a table (header and body), for a table without its own rows function
  function tableRows(table) {
    return [...table.querySelectorAll("tr")].filter(tr => tr.offsetParent !== null || !table.offsetParent)
      .map(tr => [...tr.children].map(td => td.innerText.replace(/\s+/g, " ").trim()));
  }

  const STYLE_PROPS = ["fill", "fill-opacity", "stroke", "stroke-width", "stroke-opacity", "stroke-dasharray",
    "stroke-linecap", "opacity", "font-family", "font-size", "font-weight", "font-style", "text-anchor",
    "dominant-baseline", "letter-spacing", "visibility", "display", "paint-order", "stroke-linejoin", "stroke-miterlimit"];
  async function toDataURL(href) {
    try { const r = await fetch(href); const b = await r.blob();
      return await new Promise(res => { const f = new FileReader(); f.onload = () => res(f.result); f.readAsDataURL(b); });
    } catch (e) { return null; }
  }
  // a standalone copy of a chart: computed styles inlined, logos embedded, a title above and the source below
  async function standalone(svg, title) {
    const vb = svg.viewBox.baseVal, w = vb && vb.width ? vb.width : svg.clientWidth, h = vb && vb.height ? vb.height : svg.clientHeight;
    const clone = svg.cloneNode(true), src = [svg, ...svg.querySelectorAll("*")], dst = [clone, ...clone.querySelectorAll("*")];
    src.forEach((el, i) => { const cs = getComputedStyle(el); const d = dst[i];
      d.removeAttribute("class"); d.setAttribute("style", STYLE_PROPS.map(p => `${p}:${cs.getPropertyValue(p)}`).join(";")); });
    for (const im of clone.querySelectorAll("image")) {
      const href = im.getAttribute("href") || im.getAttribute("xlink:href");
      if (href && !href.startsWith("data:")) { const d = await toDataURL(new URL(href, location.href).href); if (d) im.setAttribute("href", d); }
    }
    const top = 34, bottom = 36, NS = "http://www.w3.org/2000/svg";
    const cs = getComputedStyle(document.body), bg = cs.backgroundColor, fg = cs.color, font = cs.fontFamily;
    const out = document.createElementNS(NS, "svg");
    out.setAttribute("xmlns", NS); out.setAttribute("viewBox", `0 0 ${w} ${h + top + bottom}`);
    out.setAttribute("width", w); out.setAttribute("height", h + top + bottom);
    const rect = document.createElementNS(NS, "rect"); rect.setAttribute("width", "100%"); rect.setAttribute("height", "100%"); rect.setAttribute("fill", bg); out.appendChild(rect);
    const text = (x, y, s, size, anchor, weight) => { const t = document.createElementNS(NS, "text");
      t.setAttribute("x", x); t.setAttribute("y", y); t.setAttribute("text-anchor", anchor); t.setAttribute("style", `fill:${fg};font-family:${font};font-size:${size}px;font-weight:${weight || 400}`);
      t.textContent = s; out.appendChild(t); };
    text(12, 23, title, 17, "start", 600);
    const g = document.createElementNS(NS, "g"); g.setAttribute("transform", `translate(0 ${top})`);
    clone.removeAttribute("width"); clone.removeAttribute("height"); clone.setAttribute("width", w); clone.setAttribute("height", h);
    g.appendChild(clone); out.appendChild(g);
    const logo = await toDataURL(BASE + "assets/llm-jp-logo.png");
    if (logo) { const im = document.createElementNS(NS, "image"); im.setAttribute("href", logo);
      im.setAttribute("x", 12); im.setAttribute("y", h + top + 8); im.setAttribute("width", 20); im.setAttribute("height", 20); out.appendChild(im); }
    text(logo ? 38 : 12, h + top + 23, "simple-evals-mm · CC BY 4.0", 12, "start", 600);
    text(w - 12, h + top + 23, `${location.host}${location.pathname} · ${today()}`, 11, "end");
    return { svg: new XMLSerializer().serializeToString(out), w, h: h + top + bottom };
  }
  async function svgBlob(svg, title) { const s = await standalone(svg, title); return new Blob([s.svg], { type: "image/svg+xml" }); }
  async function pngBlob(svg, title, scale) {
    const s = await standalone(svg, title);
    const img = new Image(); const url = URL.createObjectURL(new Blob([s.svg], { type: "image/svg+xml" }));
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = url; });
    const c = document.createElement("canvas"); c.width = s.w * scale; c.height = s.h * scale;
    const ctx = c.getContext("2d"); ctx.scale(scale, scale); ctx.drawImage(img, 0, 0); URL.revokeObjectURL(url);
    return await new Promise(res => c.toBlob(res, "image/png"));
  }

  const ICON = {
    share: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 12v7a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-7M16 6l-4-4-4 4M12 2v13"/></svg>',
    download: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4M7 10l5 5 5-5M12 15V3"/></svg>',
    x: '<svg viewBox="0 0 24 24" aria-hidden="true" class="fill"><path d="M17.8 3h3.1l-6.8 7.7 8 10.3h-6.3l-4.9-6.3L5.3 21H2.2l7.2-8.3L1.8 3h6.4l4.4 5.8L17.8 3Zm-1.1 16.2h1.7L7.3 4.7H5.5l11.2 14.5Z"/></svg>',
    instagram: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="5"/><circle cx="12" cy="12" r="4"/><circle cx="17.5" cy="6.5" r="0.6" class="fill"/></svg>',
    link: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg>',
    image: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="m3 16 5-5 4 4 3-3 6 6"/></svg>',
    table: '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 4v16"/></svg>',
  };
  function closeMenus(except) { document.querySelectorAll(".share-menu.open").forEach(m => m !== except && m.classList.remove("open")); }
  document.addEventListener("click", e => { if (!e.target.closest(".share-menu")) closeMenus(null); });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeMenus(null); });

  // two buttons under `after`: Share (X, Instagram, copy link) and Download (PNG / SVG with `svg`; CSV with `csv`
  // (rows function) or `table`). The image title is fixed per chart (no list of selections that could overflow).
  function bar(after, opt) {
    const id = opt.id, nm = () => (typeof opt.name === "function" ? opt.name() : opt.name) || id;
    const title = () => (typeof opt.title === "function" ? opt.title() : opt.title) || nm();
    const svg = () => typeof opt.svg === "string" ? document.getElementById(opt.svg) : opt.svg;
    const png = async () => new File([await pngBlob(svg(), title(), 2)], `${nm()}-${today()}.png`, { type: "image/png" });
    const b = document.createElement("div"); b.className = "share-bar";
    const menu = (icon, label, items) => {
      const w = document.createElement("div"); w.className = "share-menu";
      w.innerHTML = `<button type="button" class="share-btn" aria-haspopup="menu" aria-expanded="false">${ICON[icon]}<span>${label}</span></button><div class="share-pop" role="menu"></div>`;
      const pop = w.querySelector(".share-pop"), btn = w.querySelector(".share-btn");
      for (const [ic, text, tip, fn] of items) {
        const it = document.createElement("button"); it.type = "button"; it.setAttribute("role", "menuitem"); it.title = tip;
        it.innerHTML = `${ICON[ic]}<span>${text}</span>`;
        it.onclick = async () => { const sp = it.querySelector("span");
          try { const msg = await fn(); if (msg) { sp.textContent = msg; setTimeout(() => { sp.textContent = text; }, 1600); } else w.classList.remove("open"); }
          catch (e) { sp.textContent = "Failed"; setTimeout(() => { sp.textContent = text; }, 1600); } };
        pop.appendChild(it);
      }
      btn.onclick = () => { const open = !w.classList.contains("open"); closeMenus(w); w.classList.toggle("open", open); btn.setAttribute("aria-expanded", String(open)); };
      b.appendChild(w);
    };
    const copyLink = () => new Promise(res => { const tmp = document.createElement("button"); copy(linkTo(id), tmp);
      setTimeout(() => res(tmp.textContent === "Copy failed" ? "Copy failed" : "Link copied"), 300); });
    const shareItems = [
      ["x", "X", "Post the link on X", () => { window.open(`https://x.com/intent/post?${new URLSearchParams({ text: `${title()} (simple-evals-mm)`, url: linkTo(id) })}`, "_blank", "noopener"); }],
    ];
    if (opt.svg) shareItems.push(["instagram", "Instagram", "Share the chart image (Instagram takes images, not links): the system share sheet on a phone, else the PNG is saved to post", async () => {
      const f = await png();
      if (navigator.canShare && navigator.canShare({ files: [f] })) { await navigator.share({ files: [f], title: title() }); return null; }
      download(f.name, f); return "PNG saved"; }]);
    shareItems.push(["link", "Copy link", "Copy a link to this view (with the current selections)", copyLink]);
    menu("share", "Share", shareItems);
    const dl = [];
    if (opt.svg) {
      dl.push(["image", "PNG", "Download this chart as a PNG image", async () => { const f = await png(); download(f.name, f); }]);
      dl.push(["image", "SVG", "Download this chart as an SVG image", async () => download(`${nm()}-${today()}.svg`, await svgBlob(svg(), title()))]);
    }
    if (opt.csv || opt.table) dl.push(["table", "CSV", "Download the values as CSV", () => {
      const rows = opt.csv ? opt.csv() : tableRows(typeof opt.table === "string" ? document.getElementById(opt.table) : opt.table);
      download(`${nm()}-${today()}.csv`, new Blob([toCSV(rows)], { type: "text/csv" })); }]);
    menu("download", opt.svg ? "Download graph" : "Download", dl);
    after.insertAdjacentElement("afterend", b);
    return b;
  }

  window.SHARE = { stateString, writeState, anchors, bar, copy, linkTo, toCSV };
})();
