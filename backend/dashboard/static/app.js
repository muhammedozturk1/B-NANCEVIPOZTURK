/* SMC Bot paneli */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const TZ = -new Date().getTimezoneOffset() * 60; // grafikte yerel saat
  const nf2 = new Intl.NumberFormat("tr-TR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const nf1 = new Intl.NumberFormat("tr-TR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  const nf0 = new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 0 });

  let S = null;            // son durum
  let tab = "positions";
  let histOffset = 0;
  let lastLogId = 0;
  let eqChart = null, eqSeries = null;
  let tradeChart = null;
  let settingsLoaded = false;
  let btShownId = null;
  let busy = false;
  let CSRF = "";

  // ---------------- biçimlendirme ----------------
  const money = (v, sign = false) => v == null || isNaN(v) ? "—" : (sign && v > 0 ? "+" : "") + nf2.format(v);
  const pct = (v, d = 1) => v == null || isNaN(v) ? "—" : "%" + (d === 1 ? nf1 : nf2).format(v);
  const rr = v => v == null || isNaN(v) ? "—" : (v > 0 ? "+" : "") + nf2.format(v) + "R";
  const cls = v => v > 0 ? "up" : v < 0 ? "down" : "";
  const qty = v => v == null || isNaN(v) ? "—" : new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 6 }).format(+(+v).toPrecision(10));
  function price(v) {
    if (v == null || isNaN(v)) return "—";
    const a = Math.abs(v);
    const d = a >= 1000 ? 1 : a >= 10 ? 2 : a >= 1 ? 4 : a >= 0.01 ? 5 : 7;
    return new Intl.NumberFormat("tr-TR", { minimumFractionDigits: d, maximumFractionDigits: d }).format(v);
  }
  const fmtTime = ms => ms ? new Date(ms).toLocaleString("tr-TR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
  const fmtClock = ms => new Date(ms).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  function dur(min) {
    if (!min && min !== 0) return "—";
    if (min < 60) return nf0.format(min) + " dk";
    if (min < 1440) return nf1.format(min / 60) + " sa";
    return nf1.format(min / 1440) + " gün";
  }
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------------- API ----------------
  async function api(path, opts = {}) {
    const method = opts.method || (opts.body ? "POST" : "GET");
    const r = await fetch(path, {
      method,
      headers: { "content-type": "application/json", ...(method !== "GET" ? { "X-CSRF-Token": CSRF } : {}) },
      body: opts.body ? JSON.stringify(opts.body) : (method === "POST" ? "{}" : undefined),
      credentials: "same-origin",
    });
    if (r.status === 401) { showLogin(); throw new Error("Giriş gerekli"); }
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) throw new Error(j.error || j.detail || ("Sunucu hatası " + r.status));
    return j;
  }
  function toast(msg, bad = false) {
    const t = $("#toast");
    t.textContent = msg; t.className = "toast show" + (bad ? " bad" : "");
    clearTimeout(t._h); t._h = setTimeout(() => t.className = "toast", 3200);
  }
  async function act(fn, okMsg) {
    if (busy) return;
    busy = true;
    try { await fn(); if (okMsg) toast(okMsg); await refresh(); }
    catch (e) { if (e.message !== "Giriş gerekli") toast(e.message, true); }
    finally { busy = false; }
  }

  function dialog({ title, text, input = null, okText = "Onayla", danger = false }) {
    return new Promise(res => {
      const d = $("#dialog"), inp = $("#dlgInput"), ok = $("[data-dlg=ok]", d);
      $("#dlgTitle").textContent = title; $("#dlgText").textContent = text;
      inp.hidden = !input; inp.value = ""; inp.placeholder = input || "";
      ok.textContent = okText; ok.className = "btn " + (danger ? "danger" : "primary");
      d.hidden = false; (input ? inp : ok).focus();
      const done = v => { d.hidden = true; d.onsubmit = null; $("[data-dlg=cancel]", d).onclick = null; res(v); };
      d.onsubmit = e => { e.preventDefault(); if (input && inp.value.trim().toUpperCase() !== input) { inp.focus(); return; } done(input ? inp.value.trim() : true); };
      $("[data-dlg=cancel]", d).onclick = () => done(false);
      d.onkeydown = e => { if (e.key === "Escape") done(false); };
    });
  }

  // ---------------- giriş (mevcut panel girişi: /login) ----------------
  function showLogin() { location.href = "/login?next=" + encodeURIComponent("/"); }

  // ---------------- üst bar / menü ----------------
  $("#btnRun").onclick = () => {
    if (!S) return;
    if (S.running) act(() => api("/api/bot/pause", { method: "POST" }), "Bot duraklatıldı");
    else act(() => api("/api/bot/start", { method: "POST" }), "Bot başlatıldı");
  };
  $("#btnEmergency").onclick = async () => {
    const n = S ? S.positions.length : 0;
    const ok = await dialog({ title: "Acil durdur", danger: true, okText: "Hepsini kapat ve durdur",
      text: `${n} açık pozisyon piyasa fiyatından kapatılacak, bekleyen girişler iptal edilecek ve bot duracak.` });
    if (ok) act(() => api("/api/bot/emergency", { method: "POST" }), "Acil durdurma uygulandı");
  };
  $("#btnMenu").onclick = e => {
    e.stopPropagation();
    const m = $("#menu"); m.hidden = !m.hidden; $("#btnMenu").setAttribute("aria-expanded", String(!m.hidden));
  };
  document.addEventListener("click", () => { $("#menu").hidden = true; });
  $("#menu").addEventListener("click", async e => {
    const a = e.target.dataset.act;
    if (a === "logout") { await fetch("/logout", { method: "POST", credentials: "same-origin" }).catch(() => {}); location.href = "/login"; }
    if (a === "reset") {
      const v = await dialog({ title: "Hesabı sıfırla", input: "SIFIRLA", danger: true, okText: "Sıfırla",
        text: "Bu moddaki tüm işlem geçmişi, equity eğrisi ve risk kilitleri silinir. Demo bakiyesi başlangıç değerine döner.\nOnaylamak için SIFIRLA yaz." });
      if (v) act(() => api("/api/reset", { body: { confirm: v } }), "Sıfırlandı");
    }
  });

  // ---------------- sekmeler ----------------
  $$(".tabs button").forEach(b => b.onclick = () => setTab(b.dataset.tab));
  function setTab(t) {
    tab = t;
    $$(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === t)));
    $$("[data-panel]").forEach(p => p.hidden = p.dataset.panel !== t);
    if (t === "history") loadHistory(true);
    if (t === "logs") loadLogs();
    if (t === "settings") loadSettings();
    if (t === "backtest") loadBacktest();
    if (t === "stats" && S) renderStats(S.stats);
  }

  // ---------------- durum ----------------
  async function refresh() {
    try { S = await api("/api/state"); }
    catch (e) { if (e.message !== "Giriş gerekli") setStatus("off", "Sunucuya ulaşılamıyor"); return; }
    render(S);
  }
  function setStatus(kind, text) { const p = $("#statusPill"); p.className = "status " + kind; $("span", p).textContent = text; }

  function render(s) {
    document.title = `${money(s.equity)} USDT · SMC Bot`;
    $("#modeband").className = "modeband " + s.mode;
    const tag = $("#modeTag"); tag.className = "mode-tag " + s.mode; tag.textContent = s.mode_tr + (s.sim ? " (simülasyon)" : "");
    if (s.risk.halted) setStatus("halt", "Risk kilidi");
    else if (s.running) setStatus("on", "Çalışıyor");
    else setStatus("", "Duraklatıldı");
    const run = $("#btnRun");
    run.textContent = s.running ? "Duraklat" : "Başlat";
    run.className = "btn " + (s.running ? "ghost" : "primary");

    renderAlerts(s);
    $("#eqValue").textContent = money(s.equity);
    $("#fBal").textContent = money(s.balance);
    const u = $("#fUpnl"); u.textContent = money(s.upnl, true); u.className = cls(s.upnl);
    const d = $("#fDay"); d.textContent = money(s.day_pnl, true); d.className = cls(s.day_pnl);
    const tot = s.start_balance != null ? s.equity - s.start_balance : s.stats.net_pnl;
    const t = $("#fTotal"); t.textContent = money(tot, true); t.className = cls(tot);
    renderMeters(s);
    $("#cPos").textContent = s.positions.length || "";
    renderPositions(s);
    renderPending(s);
    if (tab === "scanner") renderScanner(s);
    if (tab === "stats") renderStats(s.stats);
    if (tab === "backtest") renderBtProgress(s.backtest);
    if (tab === "settings" && settingsLoaded) renderModeBox(s);
  }

  function renderAlerts(s) {
    const a = [];
    if (s.sim) a.push(["info", "Simülasyon modu: veriler sentetik, gerçek piyasa değil (SMCBOT_OFFLINE_SIM)."]);
    if (!s.leader) a.push(["info", "Bu sunucu kopyası yedekte: bot şu an Render'daki diğer kopyada çalışıyor (yeni sürüm yayına alınırken olur). Birkaç saniye içinde devralınır."]);
    if (s.risk.halted) a.push(["bad", "Bot risk koruması nedeniyle durdu: " + s.risk.halt_reason, "unlock"]);
    if (s.risk.cooldown_until > s.server_time) a.push(["", `Art arda zararlar nedeniyle soğuma süresi: ${fmtTime(s.risk.cooldown_until)} saatine kadar yeni işlem açılmaz.`]);
    if (s.health.geo_blocked) a.push(["bad", "Binance bu sunucunun konumunu engelliyor (HTTP 451). Render'da servisi Frankfurt veya Singapore bölgesinde oluştur."]);
    if (s.health.live_error) a.push(["bad", s.health.live_error]);
    if (!s.health.ws && !s.sim) a.push(["", "Canlı fiyat akışı bağlı değil; yedek olarak REST ile fiyat çekiliyor."]);
    if (s.mode === "live") a.push(["bad", "GERÇEK MOD: işlemler gerçek parayla yapılıyor."]);
    $("#alerts").innerHTML = a.map(([k, m, btn]) => `<div class="alert ${k}"><span>${esc(m)}</span>${btn === "unlock" ? '<button class="btn small" data-unlock>Kilidi kaldır</button>' : ""}</div>`).join("");
    const ub = $("[data-unlock]");
    if (ub) ub.onclick = async () => {
      const ok = await dialog({ title: "Risk kilidini kaldır", text: "Kilit, zirveden düşüş limitine ulaşıldığı için kondu. Kaldırırsan zirve bugünkü equity'den yeniden ölçülür. Önce nedenini İstatistik ve Geçmiş sekmelerinden incelemeni öneririm.", okText: "Kilidi kaldır" });
      if (ok) act(() => api("/api/risk/unlock", { method: "POST" }), "Kilit kaldırıldı");
    };
  }

  function meter(id, frac, text) {
    const el = $(id); $("b", el).textContent = text;
    const i = $("i", el); const f = Math.max(0, Math.min(1, frac || 0));
    i.style.width = (f * 100).toFixed(1) + "%";
    i.className = f >= 0.85 ? "bad" : f >= 0.5 ? "warn" : "";
  }
  function renderMeters(s) {
    const lim = Math.abs(s.day_limit || 0);
    const used = s.day_pnl < 0 ? -s.day_pnl : 0;
    meter("#mDay", lim ? used / lim : 0, `${money(-used)} / ${money(-lim)}`);
    const st = settingsCache?.settings;
    const maxdd = st ? st.max_drawdown_pct : 30;
    meter("#mDD", (s.risk.drawdown_pct || 0) / maxdd, `${pct(s.risk.drawdown_pct)} / ${pct(maxdd, 1)}`);
    const maxp = st ? st.max_open_positions : 3;
    meter("#mPos", s.positions.length / maxp, `${s.positions.length} / ${maxp}`);
  }

  // ---------------- pozisyonlar ----------------
  function track(t, mark) {
    const L = t.side === "LONG";
    const lo = L ? t.sl_initial : t.tp2, hi = L ? t.tp2 : t.sl_initial;
    const span = hi - lo || 1;
    const p = v => Math.max(0, Math.min(100, ((v - lo) / span) * 100));
    const e = p(t.entry_price), tp1 = p(t.tp1), m = mark != null ? p(mark) : e;
    const sl = p(t.sl);
    const leftLbl = L ? "Stop" : "TP2", rightLbl = L ? "TP2" : "Stop";
    return `<div class="track" style="--e:${e}%">
      <div class="rail"></div>
      <span class="lbl l0" style="left:0">${leftLbl}</span><span class="lbl l1" style="left:100%">${rightLbl}</span>
      <span class="lbl" style="left:${e}%">Giriş</span>
      <div class="tick entry" style="left:${e}%"></div>
      <div class="tick ${t.tp1_hit ? "hit" : ""}" style="left:${tp1}%" title="TP1"></div>
      ${t.sl !== t.sl_initial ? `<div class="tick" style="left:${sl}%" title="Güncel stop"></div>` : ""}
      <div class="dot" style="left:${m}%"></div></div>`;
  }
  function renderPositions(s) {
    const el = $("#posList");
    if (!s.positions.length) {
      el.innerHTML = `<p class="empty">${s.running ? "Açık pozisyon yok. Bot her mum kapanışında kurulum arıyor." : "Açık pozisyon yok. Yeni işlem aranması için Başlat'a bas."}</p>`;
      return;
    }
    el.innerHTML = s.positions.map(t => `
      <article class="pos" data-trade="${t.id}">
        <div class="pos-top"><span class="sym">${esc(t.symbol)}</span><span class="side ${t.side}">${t.side}</span>
          <span class="muted small">${t.leverage}x</span>
          <div class="pos-pnl ${cls(t.upnl)}"><b>${money(t.upnl, true)}</b><span>${rr(t.upnl_r)}</span></div></div>
        ${track(t, t.mark)}
        <div class="pos-grid">
          <div><span>Giriş</span><b>${price(t.entry_price)}</b></div>
          <div><span>Anlık</span><b>${price(t.mark)}</b></div>
          <div><span>Stop</span><b>${price(t.sl)}</b></div>
          <div><span>Hedef</span><b>${price(t.tp2)}</b></div>
          <div><span>Miktar</span><b>${qty(t.qty_open)}</b></div>
          <div><span>Teminat</span><b>${money(t.margin)}</b></div>
          <div><span>Risk</span><b>${money(t.risk_amount)}</b></div>
          <div><span>Skor</span><b>${nf0.format(t.score)}</b></div>
        </div>
        <div class="pos-foot"><span class="muted small">${t.tp1_hit ? "TP1 alındı, stop girişte" : "Açılış " + fmtTime(t.entry_time)}</span>
          <button class="btn small danger" data-close-trade="${t.id}">Pozisyonu kapat</button></div>
      </article>`).join("");
  }
  function renderPending(s) {
    const el = $("#pendList");
    if (!s.pending.length) { el.innerHTML = `<p class="empty">Bekleyen kurulum yok.</p>`; return; }
    el.innerHTML = s.pending.map(t => `
      <div class="line" data-trade="${t.id}">
        <div class="who"><span class="sym">${esc(t.symbol)}</span><span class="side ${t.side}">${t.side}</span><span class="score ${t.score >= 75 ? "hi" : ""}">${nf0.format(t.score)}</span></div>
        <div class="meta">Giriş ${price(t.entry_price)} · anlık ${price(t.mark)}<br>Son geçerlilik ${fmtTime(t.expiry_time)}</div>
        <div class="res"><button class="btn small" data-close-trade="${t.id}" data-pending="1">İptal et</button></div>
      </div>`).join("");
  }
  document.addEventListener("click", async e => {
    const c = e.target.closest("[data-close-trade]");
    if (c) {
      e.stopPropagation();
      const id = +c.dataset.closeTrade;
      if (c.dataset.pending) return act(() => api(`/api/trade/${id}/close`, { method: "POST" }), "Kurulum iptal edildi");
      const ok = await dialog({ title: "Pozisyonu kapat", text: "Pozisyon piyasa fiyatından hemen kapatılacak.", okText: "Kapat", danger: true });
      if (ok) act(() => api(`/api/trade/${id}/close`, { method: "POST" }), "Pozisyon kapatıldı");
      return;
    }
    const tr = e.target.closest("[data-trade]");
    if (tr) openTrade(+tr.dataset.trade);
    const bt = e.target.closest("[data-bt]");
    if (bt && btTrades) openTradeObj(btTrades[+bt.dataset.bt], true);
  });

  // ---------------- geçmiş ----------------
  async function loadHistory(reset) {
    if (reset) histOffset = 0;
    const j = await api(`/api/trades?status=closed,cancelled&limit=50&offset=${histOffset}`).catch(() => null);
    if (!j) return;
    const el = $("#histList");
    const rows = j.trades.map(t => {
      if (t.status === "cancelled") return `
        <div class="line" data-trade="${t.id}"><div class="who"><span class="sym">${esc(t.symbol)}</span><span class="side ${t.side}">${t.side}</span></div>
        <div class="meta">İptal: ${esc(t.exit_reason || "")}</div><div class="res"><span>${fmtTime(t.exit_time || t.setup_time)}</span></div></div>`;
      return `
        <div class="line" data-trade="${t.id}"><div class="who"><span class="sym">${esc(t.symbol)}</span><span class="side ${t.side}">${t.side}</span></div>
        <div class="meta">${esc(t.exit_reason || "")}<br>${fmtTime(t.entry_time)} → ${fmtTime(t.exit_time)}</div>
        <div class="res ${cls(t.pnl)}"><b>${money(t.pnl, true)}</b><span>${rr(t.r_multiple)}</span></div></div>`;
    }).join("");
    if (reset) el.innerHTML = rows || `<p class="empty">Henüz kapanan işlem yok.</p>`;
    else el.insertAdjacentHTML("beforeend", rows);
    histOffset += j.trades.length;
    $("#histMore").hidden = j.trades.length < 50;
    $("#histCount").textContent = S ? `${S.stats.trades} kapanan işlem` : "";
  }
  $("#histMore").onclick = () => loadHistory(false);

  // ---------------- tarayıcı ----------------
  function renderScanner(s) {
    const sc = s.scan;
    $("#scanInfo").innerHTML = `
      <div><span>Durum</span><b>${esc(sc.status)}</b></div>
      <div><span>Son tarama</span><b>${sc.last_scan ? fmtClock(sc.last_scan) : "—"}</b></div>
      <div><span>Sonraki</span><b>${sc.next_scan ? fmtClock(sc.next_scan) : "—"}</b></div>
      <div><span>Taranan coin</span><b>${sc.scanned || sc.universe}</b></div>
      <div><span>Zaman dilimi</span><b>${sc.ltf} giriş / ${sc.htf} trend</b></div>
      <div><span>Süre</span><b>${sc.duration ? nf2.format(sc.duration) + " sn" : "—"}</b></div>`;
    const el = $("#candList");
    if (!s.candidates.length) { el.innerHTML = `<p class="empty">Henüz aday yok. İlk tarama mum kapanışında yapılır.</p>`; return; }
    el.innerHTML = s.candidates.map(c => `
      <div class="cand">
        <div class="cand-top"><span class="sym">${esc(c.symbol)}</span><span class="side ${c.side}">${c.side}</span>
          <span class="score ${c.score >= 75 ? "hi" : ""}" title="Yapı ${c.structure} + akış ${c.flow}">${nf0.format(c.score)}</span>
          <span class="muted small">${fmtClock(c.time)} · RR ${nf1.format(c.rr)}</span>
          <span class="cand-status">${esc(c.status)}</span></div>
        <ul>${c.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul>
      </div>`).join("");
  }

  // ---------------- istatistik ----------------
  function kpis(st, extra = []) {
    const items = [
      ["İşlem", nf0.format(st.trades)],
      ["Kazanma oranı", pct(st.winrate)],
      ["Profit factor", st.profit_factor == null ? "—" : nf2.format(st.profit_factor)],
      ["Ortalama sonuç", rr(st.avg_r)],
      ["Net K/Z", money(st.net_pnl, true), cls(st.net_pnl)],
      ["Maks. düşüş", pct(st.max_drawdown_pct)],
      ["En uzun kayıp serisi", nf0.format(st.max_losing_streak)],
      ["Ort. kazanç / kayıp", `${rr(st.avg_win_r)} / ${rr(st.avg_loss_r)}`],
      ["Ort. süre", dur(st.avg_duration_min)],
      ["Ödenen komisyon", money(st.fees)],
      ...extra,
    ];
    return items.map(([k, v, c]) => `<div class="kpi"><span>${k}</span><b class="${c || ""}">${v}</b></div>`).join("");
  }
  function hist(st) {
    const mx = Math.max(1, ...st.r_hist.map(h => h.count));
    return st.r_hist.map((h, i) => `<div class="bar-row"><span>${h.label}</span><div class="b"><i class="${i < 2 ? "b-neg" : i > 2 ? "b-pos" : ""}" style="width:${h.count / mx * 100}%"></i></div><b>${h.count}</b></div>`).join("");
  }
  function renderStats(st) {
    if (!st) return;
    $("#statsGrid").innerHTML = kpis(st);
    $("#rHist").innerHTML = st.trades ? hist(st) : `<p class="empty">Veri yok.</p>`;
    $("#bySide").innerHTML = ["LONG", "SHORT"].map(k => {
      const v = st.by_side[k];
      return v ? `<div class="kv"><span>${k}</span><b>${v.trades} işlem · ${pct(v.winrate, 0)} · ${rr(v.avg_r)} · <span class="${cls(v.pnl)}">${money(v.pnl, true)}</span></b></div>` : "";
    }).join("") || `<p class="empty">Veri yok.</p>`;
    $("#byReason").innerHTML = Object.entries(st.by_reason).map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><b>${v}</b></div>`).join("") || `<p class="empty">Veri yok.</p>`;
    $("#bySymbol").innerHTML = st.by_symbol.map(v => `<div class="kv"><span>${esc(v.symbol)}</span><b>${v.trades} işlem · ${v.wins} kazanç · <span class="${cls(v.pnl)}">${money(v.pnl, true)}</span></b></div>`).join("") || `<p class="empty">Veri yok.</p>`;
  }

  // ---------------- equity grafiği ----------------
  function chartOpts(el, h) {
    return {
      width: el.clientWidth, height: h,
      layout: { background: { type: "solid", color: "transparent" }, textColor: "#8C9AB3", fontFamily: "Barlow Semi Condensed, Barlow, sans-serif", attributionLogo: false },
      grid: { vertLines: { color: "rgba(36,52,79,.5)" }, horzLines: { color: "rgba(36,52,79,.5)" } },
      rightPriceScale: { borderColor: "#24344F" }, timeScale: { borderColor: "#24344F", timeVisible: true, secondsVisible: false },
      crosshair: { mode: 0 }, localization: { locale: "tr-TR" },
    };
  }
  async function loadEquity() {
    const j = await api("/api/equity").catch(() => null);
    if (!j || !window.LightweightCharts) return;
    const el = $("#eqChart");
    const pts = j.points.map(p => ({ time: Math.floor(p.ts / 1000) + TZ, value: p.equity }));
    const dedup = []; for (const p of pts) { if (dedup.length && dedup[dedup.length - 1].time >= p.time) continue; dedup.push(p); }
    $("#eqEmpty").hidden = dedup.length >= 2;
    if (!eqChart) {
      eqChart = LightweightCharts.createChart(el, { ...chartOpts(el, el.clientHeight || 150), handleScroll: false, handleScale: false });
      eqSeries = eqChart.addAreaSeries({ lineColor: "#D9A441", topColor: "rgba(217,164,65,.28)", bottomColor: "rgba(217,164,65,0)", lineWidth: 2,
        priceFormat: { type: "price", precision: 2, minMove: 0.01 } });
      new ResizeObserver(() => eqChart.applyOptions({ width: el.clientWidth })).observe(el);
    }
    eqSeries.setData(dedup);
    eqChart.timeScale().fitContent();
  }

  // ---------------- işlem detayı ----------------
  async function openTrade(id) {
    try { const t = await api(`/api/trade/${id}`); openTradeObj(t, false); }
    catch (e) { toast(e.message, true); }
  }
  async function openTradeObj(t, isBacktest) {
    const sh = $("#sheet");
    sh.hidden = false;
    const live = t.status === "open" || t.status === "pending";
    $("#sheetTitle").innerHTML = `${esc(t.symbol)} <span class="side ${t.side}">${t.side}</span> ${isBacktest ? '<span class="muted small">geçmiş test</span>' : ""}`;
    const ann = t.annotations || {};
    const tf = ann.tf || (S && S.scan.ltf) || "15m";
    const ims = { "1m": 6e4, "5m": 3e5, "15m": 9e5, "30m": 18e5, "1h": 36e5, "4h": 144e5 }[tf] || 9e5;
    const t0 = Math.min(ann.pool?.time || Infinity, ann.sweep?.time || Infinity, t.setup_time || Infinity, t.entry_time || Infinity);
    const startMs = (isFinite(t0) ? t0 : Date.now()) - 40 * ims;
    const endMs = (t.exit_time ? t.exit_time + 25 * ims : (S ? S.server_time : Date.now()));
    renderTradeBody(t, live);
    const box = $("#sheetChart");
    box.innerHTML = '<p class="muted small" style="padding:20px">Grafik yükleniyor…</p>';
    let candles = [];
    try { candles = (await api(`/api/candles?symbol=${encodeURIComponent(t.symbol)}&interval=${tf}&start=${Math.floor(startMs)}&end=${Math.floor(endMs)}`)).candles; }
    catch (e) { box.innerHTML = `<p class="muted small" style="padding:20px">Grafik verisi alınamadı: ${esc(e.message)}</p>`; return; }
    drawTradeChart(box, candles, t, ann, ims);
  }
  function nearestBar(candles, ms) {
    if (!ms || !candles.length) return null;
    const s = Math.floor(ms / 1000);
    let best = candles[0].time;
    for (const c of candles) { if (c.time <= s) best = c.time; else break; }
    return best + TZ;
  }
  function drawTradeChart(box, candles, t, ann, ims) {
    box.innerHTML = "";
    if (tradeChart) { tradeChart.remove(); tradeChart = null; }
    if (!window.LightweightCharts || !candles.length) { box.innerHTML = '<p class="muted small" style="padding:20px">Grafik verisi yok.</p>'; return; }
    tradeChart = LightweightCharts.createChart(box, chartOpts(box, box.clientHeight || 380));
    const prec = (() => { const a = Math.abs(t.entry_price || candles[0].close); return a >= 1000 ? 1 : a >= 10 ? 2 : a >= 1 ? 4 : a >= 0.01 ? 5 : 7; })();
    const cs = tradeChart.addCandlestickSeries({ upColor: "#3DCB9A", downColor: "#F0606A", borderVisible: false, wickUpColor: "#3DCB9A", wickDownColor: "#F0606A",
      priceFormat: { type: "price", precision: prec, minMove: Math.pow(10, -prec) } });
    cs.setData(candles.map(c => ({ ...c, time: c.time + TZ })));
    const L = t.side === "LONG";
    const line = (p, color, title, style = 0, width = 1, axis = true) => p && cs.createPriceLine({ price: p, color, lineWidth: width, lineStyle: style, axisLabelVisible: axis, title });
    const legend = [];
    if (ann.zone) { line(ann.zone.top, "rgba(111,163,224,.9)", ann.zone.type, 2, 1, false); line(ann.zone.bottom, "rgba(111,163,224,.9)", "", 2, 1, false); legend.push(["#6FA3E0", `${ann.zone.type} bölgesi`]); }
    if (ann.pool) { line(ann.pool.price, "#8C9AB3", ann.pool.label, 1, 1, false); legend.push(["#8C9AB3", `Süpürülen likidite (${ann.pool.label})`]); }
    if (ann.mss) { line(ann.mss.price, "rgba(230,236,245,.55)", "MSS", 3, 1, false); legend.push(["#E6ECF5", "MSS seviyesi"]); }
    line(t.entry_price, "#D9A441", "Giriş", 0, 2); legend.push(["#D9A441", "Giriş"]);
    line(t.sl_initial || t.sl, "#F0606A", "Stop", 0, 2); legend.push(["#F0606A", "Stop"]);
    if (t.sl && t.sl_initial && t.sl !== t.sl_initial) line(t.sl, "#F0606A", "Başabaş stop", 2, 1, false);
    line(t.tp1, "rgba(61,203,154,.8)", "TP1", 2);
    line(t.tp2, "#3DCB9A", "TP2", 0, 2); legend.push(["#3DCB9A", "TP1 / TP2"]);
    (ann.liq_clusters || []).slice().sort((a, b) => b.w - a.w).slice(0, 3).forEach(c => line(c.price, "rgba(176,132,255,.45)", "", 1, 1, false));
    if ((ann.liq_clusters || []).length) legend.push(["#B084FF", "Tahmini likidasyon kümeleri"]);
    const mk = [];
    const add = (ms, pos, color, shape, text) => { const tm = nearestBar(candles, ms); if (tm) mk.push({ time: tm, position: pos, color, shape, text }); };
    if (ann.sweep) add(ann.sweep.time, L ? "belowBar" : "aboveBar", "#D9A441", "circle", "Likidite avı");
    if (ann.mss) add(ann.mss.time, L ? "belowBar" : "aboveBar", "#E6ECF5", "square", "MSS");
    if (t.entry_time) add(t.entry_time, L ? "belowBar" : "aboveBar", "#D9A441", L ? "arrowUp" : "arrowDown", "Giriş");
    (t.meta?.partials || []).forEach(p => add(p.time, L ? "aboveBar" : "belowBar", "#3DCB9A", "circle", "TP1"));
    if (t.exit_time && t.status === "closed") add(t.exit_time, L ? "aboveBar" : "belowBar", (t.r_multiple || 0) >= 0 ? "#3DCB9A" : "#F0606A", L ? "arrowDown" : "arrowUp", "Çıkış " + rr(t.r_multiple));
    mk.sort((a, b) => a.time - b.time);
    cs.setMarkers(mk);
    tradeChart.timeScale().fitContent();
    new ResizeObserver(() => tradeChart && tradeChart.applyOptions({ width: box.clientWidth })).observe(box);
    $("#sheetLegend").innerHTML = legend.map(([c, l]) => `<span><i style="background:${c}"></i>${esc(l)}</span>`).join("");
  }
  function renderTradeBody(t, live) {
    const reasons = t.reasons || [];
    const durMin = t.exit_time && t.entry_time ? (t.exit_time - t.entry_time) / 60000 : null;
    let outcome = "";
    if (t.status === "closed") {
      const win = (t.pnl ?? t.r_multiple) > 0;
      outcome = `<div class="outcome ${win ? "win" : "loss"}"><b>${t.pnl != null ? money(t.pnl, true) + " USDT · " : ""}${rr(t.r_multiple)}</b><br>
        <span class="muted">Çıkış nedeni: ${esc(t.exit_reason || "—")}. ${t.tp1_hit ? "TP1'de kısmi kâr alınmış, stop girişe çekilmişti." : "TP1'e ulaşılmadı."}
        İşlem boyunca en iyi ${rr(t.mfe)}, en kötü ${rr(-(t.mae || 0))} görüldü.</span></div>`;
    } else if (t.status === "cancelled") {
      outcome = `<div class="outcome"><b>Kurulum iptal edildi</b><br><span class="muted">${esc(t.exit_reason || "")}</span></div>`;
    } else if (t.status === "open") {
      outcome = `<div class="outcome"><b>Pozisyon açık</b><br><span class="muted">Anlık fiyat ${price(t.mark)}. Stop ${price(t.sl)}, hedef ${price(t.tp2)}.</span></div>`;
    } else {
      outcome = `<div class="outcome"><b>Giriş bekleniyor</b><br><span class="muted">Fiyat ${price(t.entry_price)} seviyesine gelirse pozisyon açılacak.</span></div>`;
    }
    $("#sheetBody").innerHTML = `
      ${outcome}
      <div class="pos-grid">
        <div><span>Skor</span><b>${nf0.format(t.score || 0)}/100</b></div>
        <div><span>Giriş</span><b>${price(t.entry_price)}</b></div>
        <div><span>Stop</span><b>${price(t.sl_initial || t.sl)}</b></div>
        <div><span>TP1 / TP2</span><b>${price(t.tp1)} / ${price(t.tp2)}</b></div>
        <div><span>Çıkış</span><b>${price(t.exit_price)}</b></div>
        <div><span>Miktar</span><b>${qty(t.qty)}</b></div>
        <div><span>Risk</span><b>${t.risk_amount ? money(t.risk_amount) : "—"}</b></div>
        <div><span>Süre</span><b>${durMin != null ? dur(durMin) : "—"}</b></div>
        <div><span>Komisyon</span><b>${t.fees != null ? money(t.fees) : "—"}</b></div>
        <div><span>Funding</span><b>${t.funding ? money(t.funding) : "—"}</b></div>
        <div><span>Kurulum</span><b>${fmtTime(t.setup_time)}</b></div>
        <div><span>Açılış</span><b>${fmtTime(t.entry_time)}</b></div>
      </div>
      <h3>Neden girildi</h3>
      <ul class="reasons">${reasons.map(r => `<li class="${r.ok === true ? "ok" : r.ok === false ? "no" : "na"}">
        <span class="ic">${r.ok === true ? "✓" : r.ok === false ? "!" : "•"}</span><span>${esc(r.text)}</span><span class="pt">${r.pts ? (r.pts > 0 ? "+" : "") + r.pts : ""}</span></li>`).join("") || '<li class="na"><span class="ic">•</span><span>Kayıt yok</span><span></span></li>'}</ul>
      ${live && t.status === "open" ? `<div class="pos-foot"><span></span><button class="btn danger" data-close-trade="${t.id}">Pozisyonu kapat</button></div>` : ""}`;
  }
  $("#sheet").addEventListener("click", e => { if (e.target.id === "sheet" || e.target.closest("[data-close]")) closeSheet(); });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#sheet").hidden) closeSheet(); });
  function closeSheet() { $("#sheet").hidden = true; if (tradeChart) { tradeChart.remove(); tradeChart = null; } }

  // ---------------- kayıtlar ----------------
  async function loadLogs() {
    const j = await api(`/api/logs?after=${lastLogId}`).catch(() => null);
    if (!j || !j.logs.length) { if (!lastLogId) $("#logList").innerHTML = '<p class="empty">Kayıt yok.</p>'; return; }
    if (!lastLogId) $("#logList").innerHTML = "";
    lastLogId = j.logs[j.logs.length - 1].id;
    $("#logList").insertAdjacentHTML("afterbegin", j.logs.slice().reverse().map(l => `
      <div class="log ${l.level}"><time title="${new Date(l.ts).toLocaleString("tr-TR")}">${fmtClock(l.ts)}</time><span>${esc(l.msg)}</span></div>`).join(""));
  }

  // ---------------- ayarlar ----------------
  let settingsCache = null;
  const GROUPS = [
    ["Risk", [
      ["risk_per_trade_pct", "İşlem başı risk (%)", 0.1], ["leverage", "Kaldıraç (x)", 1], ["max_open_positions", "Maks. açık pozisyon", 1],
      ["max_same_direction", "Aynı yönde maks. pozisyon", 1], ["daily_loss_limit_pct", "Günlük zarar limiti (%)", 0.5],
      ["max_consecutive_losses", "Art arda stop limiti", 1], ["cooldown_hours", "Soğuma süresi (saat)", 1],
      ["max_drawdown_pct", "Zirveden düşüşte durdur (%)", 1], ["min_stop_pct", "Min. stop mesafesi (%)", 0.05], ["max_stop_pct", "Maks. stop mesafesi (%)", 0.1]]],
    ["Strateji", [
      ["entry_mode", "Giriş yöntemi", { ce: "FVG ortası (CE)", edge: "FVG'ye ilk temas", market: "MSS kapanışında piyasa" }],
      ["ltf", "Giriş zaman dilimi", { "5m": "5 dakika", "15m": "15 dakika", "1h": "1 saat" }],
      ["htf", "Trend zaman dilimi", { "1h": "1 saat", "4h": "4 saat" }],
      ["min_score", "Min. skor (0–100)", 1], ["min_rr", "Min. ödül/risk", 0.1], ["tp1_r", "TP1 mesafesi (R)", 0.1],
      ["tp1_close_pct", "TP1'de kapatılacak (%)", 5], ["setup_expiry_bars", "Girişi bekleme (mum)", 1], ["universe_size", "Taranacak coin sayısı", 1],
      ["require_liquidity_target", "Hedef olarak likidite seviyesi zorunlu", "check"], ["notify_setups", "Bulunan kurulumları da Telegram'a gönder", "check"]]],
    ["Demo ve maliyet", [
      ["paper_start_balance", "Demo başlangıç bakiyesi (USDT)", 10], ["taker_fee_pct", "Komisyon (%)", 0.01], ["slippage_pct", "Kayma (%)", 0.01],
      ["backtest_min_score", "Geçmiş test min. skor", 1]]],
  ];
  async function loadSettings() {
    try { settingsCache = await api("/api/settings"); } catch { return; }
    const st = settingsCache.settings;
    $("#setForm").innerHTML = GROUPS.map(([g, fields]) => `<fieldset><legend>${g}</legend>${fields.map(([k, label, kind]) => {
      if (kind === "check") return `<label class="check"><input type="checkbox" name="${k}" ${st[k] ? "checked" : ""}> ${label}</label>`;
      if (typeof kind === "object") return `<label>${label}<select name="${k}">${Object.entries(kind).map(([v, l]) => `<option value="${v}" ${String(st[k]) === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>`;
      return `<label>${label}<input type="number" name="${k}" step="${kind}" value="${st[k]}"></label>`;
    }).join("")}${g === "Risk" ? '<p class="hint" id="riskHint"></p>' : ""}</fieldset>`).join("") +
      `<div class="set-actions"><button class="btn primary" type="submit">Ayarları kaydet</button><button class="btn ghost" type="button" id="setReset">Varsayılanlara dön</button></div>`;
    const upd = () => {
      const r = parseFloat($("[name=risk_per_trade_pct]").value) || 0;
      const five = (1 - Math.pow(1 - r / 100, 5)) * 100;
      $("#riskHint").textContent = r > 2 ? `Uyarı: %${nf1.format(r)} riskle art arda 5 stop kasanın yaklaşık %${nf0.format(five)}'ini götürür. Demoda bile %1–2 önerilir.` : "";
    };
    $("[name=risk_per_trade_pct]").oninput = upd; upd();
    $("#setReset").onclick = () => {
      const d = settingsCache.defaults;
      $$("#setForm [name]").forEach(i => { if (i.type === "checkbox") i.checked = !!d[i.name]; else i.value = d[i.name]; });
      upd(); toast("Varsayılanlar yüklendi, kaydetmeyi unutma");
    };
    settingsLoaded = true;
    if (S) { renderModeBox(S); renderConn(S); }
  }
  $("#setForm").addEventListener("submit", e => {
    e.preventDefault();
    const body = {};
    $$("#setForm [name]").forEach(i => body[i.name] = i.type === "checkbox" ? i.checked : i.value);
    act(async () => { settingsCache.settings = (await api("/api/settings", { body })).settings; }, "Ayarlar kaydedildi");
  });
  function renderModeBox(s) {
    const g = s.gate;
    const gateHtml = g ? `<ul class="gate">
        <li class="${g.trades >= g.need_trades ? "ok" : ""}">Demo işlem sayısı: ${g.trades} / ${g.need_trades}</li>
        <li class="${g.pf >= g.need_pf ? "ok" : ""}">Profit factor: ${nf2.format(g.pf || 0)} / ${nf2.format(g.need_pf)}</li>
        <li class="${g.net_pnl > 0 ? "ok" : ""}">Demo net K/Z: ${money(g.net_pnl, true)} (pozitif olmalı)</li></ul>` : "";
    const opt = (m, title, text, extra = "") => `<button class="mode-opt ${m} ${s.mode === m ? "cur" : ""}" data-mode="${m}" ${s.mode === m ? "disabled" : ""}>
        <b>${title}${s.mode === m ? " (şu an)" : ""}</b><p>${text}</p>${extra}</button>`;
    $("#modeBox").innerHTML =
      opt("paper", "Demo", "Gerçek piyasa fiyatları, sanal bakiye. Komisyon ve kayma dahil. Para riski yok.") +
      opt("testnet", "Binance Testnet", s.keys.testnet ? "Binance'in deneme borsasında gerçek emirler. Emir altyapısını test etmek için." : "BINANCE_TESTNET_API_KEY tanımlı değil.") +
      opt("live", "Gerçek", (s.keys.live ? "Gerçek parayla işlem. Kilit koşulları sağlanınca açılır." : "BINANCE_API_KEY tanımlı değil.") + (g && g.override ? " (Kilit ortam değişkeniyle devre dışı bırakılmış.)" : ""), gateHtml);
    $$("#modeBox [data-mode]").forEach(b => b.onclick = async () => {
      const m = b.dataset.mode;
      if (m === "live") {
        const v = await dialog({ title: "Gerçek moda geç", input: "GERÇEK", danger: true, okText: "Gerçek moda geç",
          text: "Bundan sonra bot gerçek parayla, kaldıraçlı işlem açacak. Kayıplar gerçek olur.\nOnaylamak için GERÇEK yaz." });
        if (v) act(() => api("/api/mode", { body: { mode: m, confirm: v } }), "Gerçek moda geçildi");
      } else {
        const ok = await dialog({ title: "Modu değiştir", text: `${m === "paper" ? "Demo" : "Testnet"} moduna geçilecek. Bot duraklatılır.`, okText: "Değiştir" });
        if (ok) act(() => api("/api/mode", { body: { mode: m } }), "Mod değişti");
      }
    });
    renderConn(s);
  }
  function renderConn(s) {
    const h = s.health;
    const row = (k, v, ok) => `<div class="kv"><span>${k}</span><span class="${ok === true ? "up" : ok === false ? "down" : "muted"}">${esc(v)}</span></div>`;
    $("#connBox").innerHTML =
      row("Binance veri", h.geo_blocked ? "Konum engeli (451)" : h.data_error ? "Hata: " + h.data_error.slice(0, 80) : "Bağlı", !h.geo_blocked && !h.data_error) +
      row("Canlı fiyat akışı", h.ws ? "Bağlı" : "Bağlı değil (REST yedeği)", h.ws) +
      row("Son 24 saatte izlenen likidasyon", money(h.liq_24h) + " USDT") +
      row("CoinGlass", h.coinglass, h.coinglass.startsWith("açık") ? true : null) +
      row("Telegram", h.telegram ? "Tanımlı" : "Tanımlı değil (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)", h.telegram) +
      row("Çalışma süresi", dur(h.uptime / 60)) +
      row("Worker (SMC Scalper)", s.worker ? (s.worker.alive === true ? "Çalışıyor" : s.worker.alive === false ?
        (s.worker.age_min == null ? "Henüz durum bildirmedi" : `${nf0.format(s.worker.age_min)} dakikadır sessiz`) : "Bilinmiyor") +
        (s.worker.smc_error ? " · son hata: " + s.worker.smc_error.slice(0, 80) : "") : "—", s.worker ? s.worker.alive : null) +
      `<div class="set-actions"><button class="btn" id="tgTest" ${h.telegram ? "" : "disabled"}>Telegram'a deneme mesajı gönder</button></div>` +
      `<p class="muted small">Grafikler <a href="https://www.tradingview.com/" target="_blank" rel="noopener" style="color:inherit">TradingView</a> Lightweight Charts ile çizilir.</p>`;
    $("#tgTest").onclick = () => act(() => api("/api/telegram/test", { method: "POST" }), "Deneme mesajı gönderildi");
  }

  // ---------------- geçmiş test ----------------
  let btTrades = null;
  $("#btForm").addEventListener("submit", e => {
    e.preventDefault();
    const f = new FormData(e.target);
    act(() => api("/api/backtest", { body: { days: +f.get("days"), symbols: +f.get("symbols") } }), "Geçmiş test başladı");
  });
  function renderBtProgress(b) {
    const p = $("#btProgress");
    p.hidden = !b.running;
    if (b.running) {
      $("i", p).style.width = (b.total ? b.done / b.total * 100 : 0) + "%";
      $("span", p).textContent = `${b.done} / ${b.total} coin ${b.symbol ? "· " + b.symbol : ""}`;
    } else if (btShownId !== "pending-check") loadBacktest();
  }
  async function loadBacktest() {
    const j = await api("/api/backtest").catch(() => null);
    if (!j) return;
    renderBtProgress.lastRunning = j.state.running;
    $("#btProgress").hidden = !j.state.running;
    const l = j.last;
    if (!l || l.id === btShownId) return;
    btShownId = l.id;
    const r = l.result, st = r.stats, pr = r.params, dg = r.diag || {};
    btTrades = r.trades.slice().reverse();
    const fill = dg.signals ? ` · ${dg.signals} sinyalin ${dg.filled}'i doldu (%${nf0.format(dg.filled / dg.signals * 100)})` : "";
    $("#btResult").innerHTML = `
      <h2>Son test: ${pr.days} gün, ${pr.symbols.length} coin, ${pr.ltf}/${pr.htf}</h2>
      <p class="muted small">${fmtTime(l.ts)} · işlem başı risk %${nf1.format(pr.risk_pct)} · min. skor ${pr.min_score}${fill}</p>
      <div class="kpis">${kpis(st, [["Getiri", pct(st.return_pct), cls(st.return_pct)], ["Son bakiye (1000'den)", money(st.final_balance)]])}</div>
      <div id="btChart" class="eq-chart" style="margin-top:14px"></div>
      <div class="two"><div><h3>Sonuç dağılımı (R)</h3><div class="bars">${st.trades ? hist(st) : '<p class="empty">İşlem yok.</p>'}</div></div>
      <div><h3>Çıkış nedenleri</h3>${Object.entries(st.by_reason).map(([k, v]) => `<div class="kv"><span>${esc(k)}</span><b>${v}</b></div>`).join("") || '<p class="empty">—</p>'}</div></div>
      <h3>İşlemler <span class="muted small">(grafiği görmek için dokun)</span></h3>
      <div class="hist-list">${btTrades.slice(0, 200).map((t, i) => `
        <div class="line" data-bt="${i}"><div class="who"><span class="sym">${esc(t.symbol)}</span><span class="side ${t.side}">${t.side}</span></div>
        <div class="meta">${esc(t.exit_reason)}<br>${fmtTime(t.entry_time)}</div>
        <div class="res ${cls(t.r_multiple)}"><b>${rr(t.r_multiple)}</b><span>${money(t.pnl, true)}</span></div></div>`).join("") || '<p class="empty">Bu aralıkta işlem çıkmadı.</p>'}</div>
      ${r.errors && r.errors.length ? `<p class="muted small">Hatalar: ${esc(r.errors.slice(0, 3).join("; "))}</p>` : ""}`;
    if (window.LightweightCharts && r.curve.length > 1) {
      const el = $("#btChart");
      const c = LightweightCharts.createChart(el, { ...chartOpts(el, 170), handleScroll: false, handleScale: false });
      const ser = c.addAreaSeries({ lineColor: "#6FA3E0", topColor: "rgba(111,163,224,.25)", bottomColor: "rgba(111,163,224,0)", lineWidth: 2 });
      const pts = []; r.curve.forEach(p => { const tm = Math.floor(p.ts / 1000) + TZ; if (!pts.length || tm > pts[pts.length - 1].time) pts.push({ time: tm, value: p.equity }); });
      ser.setData(pts); c.timeScale().fitContent();
    }
  }

  // ---------------- başlat ----------------
  let timers = [];
  async function start() {
    try {
      const ses = await api("/api/session");
      CSRF = ses.csrf; $("#whoami").textContent = ses.user ? "(" + ses.user + ")" : "";
      S = await api("/api/state");
    } catch (e) {
      if (e.message !== "Giriş gerekli") {
        $("#app").hidden = false;
        $("#alerts").innerHTML = `<div class="alert bad"><span>${esc(e.message)}</span></div>`;
      }
      return;
    }
    $("#app").hidden = false;
    if (!settingsCache) { try { settingsCache = await api("/api/settings"); } catch {} }
    render(S);
    loadEquity();
    timers.forEach(clearInterval);
    timers = [
      setInterval(refresh, 2500),
      setInterval(loadEquity, 30000),
      setInterval(() => { if (tab === "logs") loadLogs(); if (tab === "history") loadHistory(true); }, 10000),
    ];
  }
  start();
})();
