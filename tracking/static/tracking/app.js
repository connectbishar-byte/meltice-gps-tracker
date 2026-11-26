/* Shared helpers for the Meltice GPS Tracker UI */
(function () {
  const cfg = window.FLEET_CFG || {tz: "Asia/Dubai", poll: 5};
  const dtf = new Intl.DateTimeFormat("en-GB", {timeZone: cfg.tz, year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false});

  const F = window.Fleet = {
    cfg,
    fmt(iso) { if (!iso) return "—"; return dtf.format(new Date(iso)).replace(",", ""); },
    ago(iso) {
      if (!iso) return "never";
      let s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
      if (s < 60) return s + " s ago";
      if (s < 3600) return Math.round(s / 60) + " min ago";
      if (s < 86400) return Math.round(s / 3600) + " h ago";
      return Math.round(s / 86400) + " days ago";
    },
    stateLabel: {moving: "Moving", idling: "Idling", parked: "Parked", nosignal: "No signal"},
    stateColor: {moving: "#16a34a", idling: "#f59e0b", parked: "#dc2626", nosignal: "#6b7280"},
    esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c])); },
    async json(url) {
      const r = await fetch(url, {credentials: "same-origin", headers: {"Accept": "application/json"}});
      if (!r.ok) throw new Error(r.status + " " + r.statusText);
      return r.json();
    },
    vehicleIcon(state, angle) {
      const c = F.stateColor[state] || "#6b7280";
      const rot = state === "moving" ? `style="transform: rotate(${angle || 0}deg)"` : "";
      const glyph = state === "moving" ? "bi-arrow-up" : (state === "nosignal" ? "bi-wifi-off" : "bi-truck");
      return L.divIcon({className: "", iconSize: [30, 30], iconAnchor: [15, 15], popupAnchor: [0, -14],
        html: `<div class="veh-icon" style="width:30px;height:30px;background:${c}"><i class="bi ${glyph}" ${rot}></i></div>`});
    },
    baseLayers(map) {
      const streets = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
        {maxZoom: 19, attribution: "&copy; OpenStreetMap contributors"});
      const sat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        {maxZoom: 19, attribution: "Imagery &copy; Esri"});
      streets.addTo(map);
      L.control.layers({"Map": streets, "Satellite": sat}, {}, {position: "topright"}).addTo(map);
    },
  };

  // light / dark / match-system theme (applied early by an inline script in base.html)
  const media = window.matchMedia ? matchMedia("(prefers-color-scheme: dark)") : null;
  const ICON = {light: "bi-sun", dark: "bi-moon-stars", auto: "bi-circle-half"};
  function applyTheme(pref) {
    const dark = pref === "dark" || (pref === "auto" && media && media.matches);
    document.documentElement.setAttribute("data-bs-theme", dark ? "dark" : "light");
    document.documentElement.dataset.themePref = pref;
    document.querySelectorAll("[data-theme-icon]").forEach(i => { i.className = "bi " + ICON[pref]; });
    document.querySelectorAll("[data-theme-set]").forEach(b => b.classList.toggle("active", b.dataset.themeSet === pref));
    document.dispatchEvent(new CustomEvent("themechange", {detail: {dark}}));
  }
  F.theme = () => document.documentElement.getAttribute("data-bs-theme");
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-theme-set]");
    if (!b) return;
    try { localStorage.setItem("mgt-theme", b.dataset.themeSet); } catch (err) { /* private mode */ }
    applyTheme(b.dataset.themeSet);
  });
  if (media && media.addEventListener) media.addEventListener("change", () => {
    if ((document.documentElement.dataset.themePref || "auto") === "auto") applyTheme("auto");
  });
  applyTheme(document.documentElement.dataset.themePref || "auto");

  // clock + receiver pill
  const clock = document.getElementById("uaeClock");
  const tick = () => { if (clock) clock.textContent = F.fmt(new Date().toISOString()) + " UAE"; };
  tick(); setInterval(tick, 1000);
  const pill = document.getElementById("rxPill");
  async function rx() {
    if (!pill || !cfg.rx) return;
    try {
      const s = await F.json(cfg.rx);
      pill.className = "rx-pill " + (s.up ? "up" : "down");
      pill.title = s.up ? `Receiver listening on ${s.host}:${s.port} — ${s.active} active connection(s)` : s.detail;
      pill.innerHTML = `<i class="bi bi-circle-fill"></i> Receiver ${s.up ? "online" : "offline"}`;
    } catch (e) { pill.className = "rx-pill down"; }
  }
  rx(); setInterval(rx, 10000);
})();
