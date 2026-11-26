/* Port terminal: live tail of /api/v1/port-events/ (everything arriving on the tracker TCP port). */
(function () {
  const cfg = window.TERM_CFG, F = window.Fleet;
  const $ = id => document.getElementById(id);
  const term = $("term"), empty = $("tEmpty");
  const MAX_LINES = 5000, SHOW_BYTES = 96;
  const tf = new Intl.DateTimeFormat("en-GB", {timeZone: F.cfg.tz, hour: "2-digit", minute: "2-digit", second: "2-digit",
    fractionalSecondDigits: 3, hour12: false});
  const df = new Intl.DateTimeFormat("en-GB", {timeZone: F.cfg.tz, year: "numeric", month: "2-digit", day: "2-digit"});
  const LABEL = {connect: "CONN", refused: "REFUSED", rx: "RX", tx: "TX", imei: "IMEI", frame: "FRAME", error: "ERROR",
    close: "CLOSE", info: "INFO"};
  const GROUP = {connect: "conn", refused: "conn", imei: "conn", close: "conn", info: "conn", rx: "rx", tx: "tx",
    frame: "frame", error: "error"};

  let events = [], lastId = null, firstId = null, paused = false, pending = [], busy = false, failures = 0;
  let filter = "", format = "hex";
  const kinds = new Set(["conn", "rx", "tx", "frame", "error"]);

  const fmtHex = (hex, mode) => {
    if (mode === "hex") return hex;
    const pairs = hex.match(/../g) || [];
    if (mode === "spaced") return pairs.join(" ");
    const ascii = pairs.map(p => { const c = parseInt(p, 16); return c >= 32 && c < 127 ? String.fromCharCode(c) : "."; }).join("");
    return pairs.join(" ") + "  |" + ascii + "|";
  };
  function dump(hex) {
    const b = hex.match(/../g) || [];
    const rows = [];
    for (let o = 0; o < b.length; o += 16) {
      const chunk = b.slice(o, o + 16);
      const h = chunk.map((x, i) => x + (i === 7 ? "  " : " ")).join("").padEnd(16 * 3 + 1);
      const a = chunk.map(x => { const c = parseInt(x, 16); return c >= 32 && c < 127 ? String.fromCharCode(c) : "."; }).join("");
      rows.push(o.toString(16).padStart(8, "0") + "  " + h + " |" + a + "|");
    }
    return rows.join("\n");
  }
  const visible = e => {
    if (!kinds.has(GROUP[e.kind] || "conn")) return false;
    if (!filter) return true;
    const f = filter.toLowerCase();
    return (e.imei || "").includes(f) || (e.remote || "").includes(f) || (e.message || "").toLowerCase().includes(f) ||
      (e.hex || "").toLowerCase().includes(f.replace(/\s+/g, ""));
  };

  function lineHtml(e) {
    const t = new Date(e.ts);
    let body;
    if (e.kind === "rx" || e.kind === "tx") {
      const shown = (e.hex || "").slice(0, SHOW_BYTES * 2);
      const more = e.size > SHOW_BYTES ? ` <span class="asc">… +${e.size - SHOW_BYTES} B (click)</span>` : "";
      body = `<span class="asc">${e.size} B${e.message ? " · " + F.esc(e.message) : ""}</span>  <span class="hexb">${F.esc(fmtHex(shown, format))}</span>${more}${e.truncated ? ' <span class="asc">(stored truncated)</span>' : ""}`;
    } else {
      body = `<span class="msg">${F.esc(e.message)}</span>`;
      if (e.packet) body += ` <a href="${cfg.raw}${e.packet}/" class="asc" onclick="event.stopPropagation()">packet #${e.packet}</a>`;
    }
    return `<span class="t" title="${df.format(t)}">${tf.format(t)}</span><span class="r" title="${F.esc(e.remote)}">${F.esc(e.remote)}</span>` +
      `<span class="im" title="${F.esc(e.imei)}">${F.esc(e.imei || "—")}</span><span class="k">${LABEL[e.kind] || e.kind}</span><span class="body">${body}</span>`;
  }
  function makeLine(e) {
    const d = document.createElement("div");
    d.className = `ln k-${e.kind} lv-${e.level}`;
    d.dataset.id = e.id;
    d.innerHTML = lineHtml(e);
    return d;
  }
  const atBottom = () => term.scrollHeight - term.scrollTop - term.clientHeight < 40;

  function append(list, prepend) {
    if (!list.length) return;
    const stick = $("tFollow").checked && atBottom();
    const frag = document.createDocumentFragment();
    list.filter(visible).forEach(e => frag.appendChild(makeLine(e)));
    if (prepend) {
      const h = term.scrollHeight;
      term.insertBefore(frag, empty.nextSibling);
      term.scrollTop += term.scrollHeight - h;
    } else term.appendChild(frag);
    while (term.querySelectorAll(".ln").length > MAX_LINES) term.querySelector(".ln").remove();
    empty.hidden = events.length > 0;
    if (!prepend && (stick || $("tFollow").checked && list === events)) term.scrollTop = term.scrollHeight;
    count();
  }
  function rerender() {
    term.querySelectorAll(".ln").forEach(n => n.remove());
    append(events);
    term.scrollTop = term.scrollHeight;
  }
  function count() {
    const shown = term.querySelectorAll(".ln").length;
    $("tCount").textContent = `${shown} shown · ${events.length} loaded` + (pending.length ? ` · ${pending.length} new while paused` : "");
  }
  function status(r, ok) {
    const live = $("tLive");
    if (!ok) { live.className = "live-dot"; $("tState").textContent = "connection to server lost, retrying…"; return; }
    live.className = "live-dot" + (r && r.up ? " on" : "");
    $("tRx").textContent = r && r.up ? "Receiver listening" : "Receiver not running";
    $("tActive").textContent = r && r.active != null ? r.active : "0";
    $("tState").textContent = paused ? "paused" : `live · updated ${tf.format(new Date()).slice(0, 8)}`;
  }

  async function poll() {
    if (busy) return;
    busy = true;
    try {
      const url = lastId == null ? `${cfg.api}?tail=300` : `${cfg.api}?after=${lastId}&limit=1000`;
      const r = await F.json(url);
      failures = 0;
      if (r.events.length) {
        if (firstId == null) firstId = r.events[0].id;
        if (paused) pending.push(...r.events);
        else { events.push(...r.events); append(r.events); }
        if (events.length > MAX_LINES) events.splice(0, events.length - MAX_LINES);
      }
      lastId = r.last_id;
      status(r.receiver, true);
      count();
    } catch (err) {
      failures++;
      status(null, false);
      if (String(err).startsWith("403") || String(err).startsWith("401")) { $("tState").textContent = "signed out, reload the page"; }
    } finally {
      busy = false;
      setTimeout(poll, failures ? Math.min(15000, cfg.poll * 2 ** failures) : cfg.poll);
    }
  }

  term.addEventListener("click", e => {
    const ln = e.target.closest(".ln");
    if (!ln || e.target.closest(".dump")) return;
    const open = ln.querySelector(".dump");
    if (open) { open.remove(); ln.classList.remove("open"); return; }
    const ev = events.find(x => String(x.id) === ln.dataset.id);
    if (!ev) return;
    const d = document.createElement("div");
    d.className = "dump";
    const head = `${df.format(new Date(ev.ts))} ${tf.format(new Date(ev.ts))}  ${LABEL[ev.kind]}  ${ev.remote}  IMEI ${ev.imei || "—"}  connection #${ev.connection ?? "—"}  event #${ev.id}`;
    d.textContent = head + "\n" + (ev.message ? ev.message + "\n" : "") + (ev.hex ? "\n" + dump(ev.hex) + `\n\n${ev.hex}` : "");
    ln.appendChild(d);
    ln.classList.add("open");
  });

  let ft;
  $("tFilter").addEventListener("input", e => { clearTimeout(ft); ft = setTimeout(() => { filter = e.target.value.trim(); rerender(); }, 200); });
  document.querySelectorAll("[data-kind]").forEach(b => b.addEventListener("click", () => {
    const k = b.dataset.kind;
    kinds.has(k) ? kinds.delete(k) : kinds.add(k);
    b.classList.toggle("active", kinds.has(k));
    b.setAttribute("aria-pressed", kinds.has(k));
    rerender();
  }));
  $("tFormat").addEventListener("change", e => { format = e.target.value; rerender(); });
  $("tPause").addEventListener("click", e => {
    paused = !paused;
    const b = e.currentTarget;
    b.innerHTML = paused ? '<i class="bi bi-play-fill"></i> Resume' : '<i class="bi bi-pause-fill"></i> Pause';
    b.setAttribute("aria-pressed", paused);
    if (!paused && pending.length) { events.push(...pending); append(pending); pending = []; }
    $("tState").textContent = paused ? "paused" : "live";
    count();
  });
  $("tClear").addEventListener("click", () => { events = []; pending = []; firstId = null; rerender(); empty.hidden = false; count(); });
  $("tOlder").addEventListener("click", async () => {
    if (firstId == null) return;
    try {
      const r = await F.json(`${cfg.api}?before=${firstId}&limit=500`);
      if (!r.events.length) { $("tState").textContent = "no older events stored"; return; }
      firstId = r.events[0].id;
      events.unshift(...r.events);
      append(r.events, true);
    } catch (err) { $("tState").textContent = "could not load older events"; }
  });
  $("tSave").addEventListener("click", () => {
    const lines = events.filter(visible).map(e => {
      const t = new Date(e.ts);
      return [df.format(t) + " " + tf.format(t), e.remote, e.imei || "-", LABEL[e.kind], e.size ? e.size + "B" : "",
        e.message || "", e.hex || ""].join("\t");
    });
    const blob = new Blob(["time\tremote\timei\tkind\tsize\tmessage\thex\n" + lines.join("\n") + "\n"], {type: "text/plain"});
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `port-${new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19)}.log`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  });

  poll();
})();
