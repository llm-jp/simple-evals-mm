// Shareable views, used by the leaderboard, Trends and Analysis pages:
//  * section links: every h2 / h3 of <main> gets an id and a "#" that puts the section's URL in the address bar and
//    copies it (with the page's current view state);
//  * view state in the query string (?k=v&...), so a section anchor (#...) can follow it; links of the earlier
//    #k=v form are still read;
//  * a bar under each chart / table (as on Epoch AI's pages): Share (copy the link to that chart in its current
//    state), PNG, SVG (charts) and CSV (the plotted / listed values).
(function () {
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
    const top = 34, bottom = 26, NS = "http://www.w3.org/2000/svg";
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
    text(w - 12, h + top + 18, `simple-evals-mm · ${location.host}${location.pathname} · ${today()}`, 11, "end");
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

  // a bar under `after`: Share always; PNG / SVG with `svg`; CSV with `csv` (rows function) or `table`
  function bar(after, opt) {
    const id = opt.id, nm = () => (typeof opt.name === "function" ? opt.name() : opt.name) || id;
    const title = () => (typeof opt.title === "function" ? opt.title() : opt.title) || nm();
    const b = document.createElement("div"); b.className = "share-bar";
    const btn = (label, tip, fn) => { const x = document.createElement("button"); x.type = "button"; x.textContent = label; x.title = tip;
      x.onclick = async () => { try { await fn(x); } catch (e) { x.textContent = "Failed"; setTimeout(() => { x.textContent = label; }, 1600); } }; b.appendChild(x); };
    btn("Share", "Copy a link to this view (with the current selections)", x => copy(linkTo(id), x));
    const svg = () => typeof opt.svg === "string" ? document.getElementById(opt.svg) : opt.svg;
    if (opt.svg) {
      btn("PNG", "Download this chart as a PNG image", async () => download(`${nm()}-${today()}.png`, await pngBlob(svg(), title(), 2)));
      btn("SVG", "Download this chart as an SVG image", async () => download(`${nm()}-${today()}.svg`, await svgBlob(svg(), title())));
    }
    if (opt.csv || opt.table) btn("CSV", "Download the values as CSV", () => {
      const rows = opt.csv ? opt.csv() : tableRows(typeof opt.table === "string" ? document.getElementById(opt.table) : opt.table);
      download(`${nm()}-${today()}.csv`, new Blob([toCSV(rows)], { type: "text/csv" })); });
    after.insertAdjacentElement("afterend", b);
    return b;
  }

  window.SHARE = { stateString, writeState, anchors, bar, copy, linkTo, toCSV };
})();
