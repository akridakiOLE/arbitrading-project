// Arbitrading Bot Dashboard - Client JS (v5.1)
//
// Νέα features:
//  - Apply Settings (Live): εφαρμόζει LIVE + NEXT_CYCLE changes χωρίς Stop/Start
//  - Resume-from-state toggle
//  - Price highlight + progress bars trigger distance
//  - On-demand ATR
//  - Traded symbols history
//  - Symbol preset dropdown + KuCoin validation button

const REFRESH_MS = 3000;

async function api(path, opts = {}) {
  opts.credentials = 'include';
  if (opts.body && typeof opts.body !== 'string') {
    opts.headers = {...(opts.headers || {}), 'Content-Type': 'application/json'};
    opts.body = JSON.stringify(opts.body);
  }
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

function fmt(v, dec = 4) {
  if (v === null || v === undefined || v === '') return '-';
  if (typeof v === 'number') {
    if (Math.abs(v) > 0 && (Math.abs(v) < 0.0001 || Math.abs(v) > 1e9)) {
      return v.toExponential(4);
    }
    return v.toFixed(dec);
  }
  return String(v);
}

function setText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

// -----------------------------------------------------------------
// Trigger distance computation (B3b)
// -----------------------------------------------------------------
function renderTriggerBoxes(s) {
  const price = (s.feed && typeof s.feed.last_price === 'number') ? s.feed.last_price : null;
  const st    = s.strategy || {};
  const ref   = (typeof st.reference_price === 'number') ? st.reference_price : null;

  // Οι πραγματικές τιμές trigger (buy_trailing_stop / sell_trailing_stop) ισχύουν
  // μόνο όταν έχει ενεργοποιηθεί ο tracker. Σε αλλιώς, υπολογίζουμε initial
  // activation thresholds από reference_price και active profit pct.
  const pctBuy  = (typeof st.active_profit_pct_buy  === 'number') ? st.active_profit_pct_buy  : null;
  const pctSell = (typeof st.active_profit_pct_sell === 'number') ? st.active_profit_pct_sell : null;

  let buyTarget  = (st.buy_activated  ? st.buy_trailing_stop  : (ref && pctBuy  !== null ? ref * (1 - pctBuy  / 100) : null));
  let sellTarget = (st.sell_activated ? st.sell_trailing_stop : (ref && pctSell !== null ? ref * (1 + pctSell / 100) : null));

  setText('t-buy-val',  buyTarget  !== null && buyTarget  !== undefined ? fmt(buyTarget,  10) : '-');
  setText('t-sell-val', sellTarget !== null && sellTarget !== undefined ? fmt(sellTarget, 10) : '-');

  // BUY trigger: απόσταση = (price - buyTarget) / price · 100  (θέλουμε να πέσει)
  // SELL trigger: απόσταση = (sellTarget - price) / price · 100 (θέλουμε να ανέβει)
  if (price !== null && buyTarget !== null && price > 0) {
    const dist = ((price - buyTarget) / price) * 100;
    setText('t-buy-dist', dist >= 0 ? dist.toFixed(2) : dist.toFixed(2));
    const pct = Math.max(0, Math.min(100, (1 - Math.min(Math.abs(dist), 10) / 10) * 100));
    document.getElementById('t-buy-bar').style.width = pct + '%';
  } else {
    setText('t-buy-dist', '-');
    document.getElementById('t-buy-bar').style.width = '0%';
  }
  if (price !== null && sellTarget !== null && price > 0) {
    const dist = ((sellTarget - price) / price) * 100;
    setText('t-sell-dist', dist >= 0 ? dist.toFixed(2) : dist.toFixed(2));
    const pct = Math.max(0, Math.min(100, (1 - Math.min(Math.abs(dist), 10) / 10) * 100));
    document.getElementById('t-sell-bar').style.width = pct + '%';
  } else {
    setText('t-sell-dist', '-');
    document.getElementById('t-sell-bar').style.width = '0%';
  }
}

function renderStatus(s) {
  const running = !!s.running;
  const badge = document.getElementById('status-badge');
  badge.textContent = running ? 'RUNNING' : 'STOPPED';
  badge.className = 'badge ' + (running ? 'badge-running' : 'badge-idle');
  if (s.last_error) {
    badge.textContent = 'ERROR';
    badge.className = 'badge badge-error';
    badge.title = s.last_error;
  } else {
    badge.title = '';
  }

  document.getElementById('btn-start').disabled      = running;
  document.getElementById('btn-stop').disabled       = !running;
  document.getElementById('btn-resset').disabled     = !running;
  document.getElementById('btn-apply-live').disabled = !running;

  setText('m-mode',       s.mode   || '-');
  setText('m-symbol',     s.symbol || '-');
  setText('m-symbol-big', s.symbol || '-');
  setText('m-ticks',      s.tick_count || 0);

  const st = s.strategy || {};
  const sn = s.snapshot || {};
  setText('m-state',    st.state || '-');
  setText('m-cycles',   st.cycle_count || 0);

  const lastPrice = (s.feed && s.feed.last_price !== null && s.feed.last_price !== undefined)
                    ? s.feed.last_price : null;
  setText('m-price',     lastPrice !== null ? fmt(lastPrice, 10) : '-');
  setText('m-price-big', lastPrice !== null ? fmt(lastPrice, 10) : '-');
  setText('m-ref',       fmt(st.reference_price, 10));
  setText('m-ratio',     fmt(sn.margin_ratio, 4));
  // v6.x: cycle-cumulative counters (δεν μηδενίζονται σε αλλαγή κατεύθυνσης).
  // Fallback στα παλιά per-direction counters αν το backend δεν τα στείλει.
  setText('m-buy-cnt',   (st.buy_count_total  !== undefined ? st.buy_count_total  : (st.buy_trigger_count  || 0)));
  setText('m-sell-cnt',  (st.sell_count_total !== undefined ? st.sell_count_total : (st.sell_trigger_count || 0)));
  setText('m-hb',        st.has_bought === true ? 'true' : (st.has_bought === false ? 'false' : '-'));
  setText('m-apb',       st.active_profit_pct_buy !== undefined ? fmt(st.active_profit_pct_buy, 2) : '-');

  setText('b-base',    fmt(sn.base_coin, 4));
  setText('b-bborrow', fmt(sn.base_debt, 4));
  setText('b-usdt',    fmt(sn.usdt, 2));
  setText('b-udebt',   fmt(sn.usdt_debt, 2));
  setText('b-vdebt',   fmt(sn.vip_debt_usdt, 2));
  // v6.x: enriched VIP holdings — qty + purchase cost + current value
  const vipEl = document.getElementById('b-vip');
  const enriched = (s.strategy && s.strategy.vip_holdings_enriched) || null;
  if (vipEl) {
    if (enriched && Object.keys(enriched).length > 0) {
      const lines = [];
      for (const [coin, info] of Object.entries(enriched)) {
        const qty   = (info.quantity      !== undefined) ? Number(info.quantity).toFixed(8) : '-';
        const cost  = (info.purchase_cost !== undefined) ? Number(info.purchase_cost).toFixed(2) : '-';
        const val   = (info.current_value !== undefined) ? Number(info.current_value).toFixed(2) : '-';
        const pnl   = (info.purchase_cost && info.current_value) ? (Number(info.current_value) - Number(info.purchase_cost)).toFixed(2) : '-';
        lines.push(`${coin}: qty=${qty} | cost=$${cost} | now=$${val} | Δ=${pnl}`);
      }
      vipEl.textContent = lines.join('\n');
      vipEl.style.whiteSpace = 'pre-line';
    } else {
      vipEl.textContent = '{}';
      vipEl.style.whiteSpace = 'normal';
    }
  }
  setText('b-grand',   fmt(st.grand_amount, 2));
  setText('b-tassets', fmt(sn.total_assets, 2));

  // Resume event display
  const ri = document.getElementById('resume-info');
  if (s.last_resume_event) {
    ri.textContent = 'Last start: ' + s.last_resume_event;
    ri.className = 'resume-info ' + (s.last_resume_event === 'resumed' ? 'resumed' : 'fresh');
  } else {
    ri.textContent = '';
  }

  // Pending NEXT_CYCLE display
  const pi = document.getElementById('pending-info');
  const pending = s.pending_next_cycle || {};
  const keys = Object.keys(pending);
  if (keys.length > 0) {
    pi.textContent = 'Pending (επόμενο SETUP): ' + keys.map(k => `${k}=${pending[k]}`).join(', ');
    pi.className = 'pending-info visible';
  } else {
    pi.textContent = '';
    pi.className = 'pending-info';
  }

  renderTriggerBoxes(s);
}

async function loadStatus() {
  try {
    const s = await api('/api/status');
    renderStatus(s);
  } catch (e) {
    console.error('status failed', e);
  }
}

async function loadConfig() {
  try {
    const cfg = await api('/api/config');
    const form = document.getElementById('cfg-form');
    for (const [k, v] of Object.entries(cfg)) {
      const el = form.elements[k];
      if (!el) continue;
      if (el.type === 'checkbox') {
        el.checked = !!v;
      } else if (k === 'vip_coins' && Array.isArray(v)) {
        el.value = v.join(',');
      } else if (typeof v === 'object' && v !== null) {
        // Arrays/objects που ΔΕΝ έχουν ειδική UI αναπαράσταση — skip
        continue;
      } else {
        el.value = (typeof v === 'boolean') ? String(v) : v;
      }
    }
    // VIP special fields (text representation)
    const pctEl  = form.elements['vip_percentages_text'];
    const prioEl = form.elements['vip_priority_list_text'];
    if (pctEl && cfg.vip_percentages && typeof cfg.vip_percentages === 'object') {
      pctEl.value = Object.entries(cfg.vip_percentages)
                          .map(([k, v]) => `${k}=${v}`).join('\n');
    }
    if (prioEl && Array.isArray(cfg.vip_priority_list)) {
      prioEl.value = cfg.vip_priority_list.join('\n');
    }
    // Sync symbol preset dropdown με το input
    syncSymbolPreset();
    // Toggle Promote 2 section based on current promote value
    togglePromote2Section();
    updateVipPercentSum();
  } catch (e) {
    console.error('config load failed', e);
  }
}

// --- Promote 2 helpers ---
function togglePromote2Section() {
  const sel = document.querySelector('[name="promote"]');
  const sec = document.getElementById('vip-config-section');
  if (!sel || !sec) return;
  const isPromote2 = sel.value === '2';
  // Κρατάμε το display:grid από CSS — χρησιμοποιούμε class toggle για visibility.
  sec.classList.toggle('hidden', !isPromote2);
  // v6.x: όταν αλλάξει promote ΑΠΟ 2 ΣΕ άλλο, καθάρισε τα VIP πεδία
  // ώστε να μη μένουν παλιές τιμές αν επανέλθεις σε promote=2.
  if (!isPromote2) {
    const form  = document.getElementById('cfg-form');
    if (form) {
      const coinsEl = form.elements['vip_coins'];
      const pctEl   = form.elements['vip_percentages_text'];
      const prioEl  = form.elements['vip_priority_list_text'];
      if (coinsEl) coinsEl.value = '';
      if (pctEl)   pctEl.value   = '';
      if (prioEl)  prioEl.value  = '';
    }
  }
}

function parseVipPercentages(text) {
  const out = {};
  if (!text) return out;
  text.split(/\r?\n/).forEach(line => {
    const [k, v] = line.split('=').map(s => s && s.trim());
    if (k && v !== undefined && v !== '') {
      const num = parseFloat(v);
      if (!isNaN(num)) out[k.toUpperCase()] = num;
    }
  });
  return out;
}

function parseVipPriority(text) {
  if (!text) return [];
  return text.split(/\r?\n/).map(s => s.trim().toUpperCase()).filter(Boolean);
}

function updateVipPercentSum() {
  const area = document.querySelector('[name="vip_percentages_text"]');
  const out  = document.getElementById('vip-pct-sum');
  if (!area || !out) return;
  const d = parseVipPercentages(area.value);
  const sum = Object.values(d).reduce((a, b) => a + b, 0);
  if (Object.keys(d).length === 0) {
    out.textContent = '';
    out.className = 'msg-inline';
    return;
  }
  if (Math.abs(sum - 100) < 0.01) {
    out.textContent = `Sum = ${sum.toFixed(2)} ✓`;
    out.className = 'msg-inline ok';
  } else {
    out.textContent = `Sum = ${sum.toFixed(2)} (χρειάζεται να είναι 100)`;
    out.className = 'msg-inline error';
  }
}

function syncSymbolPreset() {
  const input  = document.querySelector('[name="symbol"]');
  const preset = document.getElementById('symbol-preset');
  if (!input || !preset) return;
  const opts = Array.from(preset.options).map(o => o.value);
  preset.value = opts.includes(input.value) ? input.value : '';
}

function getConfigFromForm() {
  const form = document.getElementById('cfg-form');
  const fd = new FormData(form);
  const o = {};
  for (const [k, v] of fd.entries()) {
    if (k === 'symbol_preset') continue;   // UI-only
    if (k === 'vip_percentages_text' || k === 'vip_priority_list_text') continue; // parsed separately
    o[k] = v;
  }
  // Types
  o.second_profit_enabled = (o.second_profit_enabled === 'true');
  o.resume_from_state     = (o.resume_from_state     === 'true');
  for (const key of ['start_base_coin','scale_base_coin','min_profit_percent','step_point',
                     'trailing_stop','margin_level','second_profit_percent','poll_interval',
                     'scale_vip_coin','min_order_usdt']) {
    if (o[key] !== undefined && o[key] !== '') o[key] = parseFloat(o[key]);
  }
  o.promote = parseInt(o.promote || '1', 10);

  // VIP fields parsing
  // vip_coins: text "BTC,ETH,SOL" → ["BTC","ETH","SOL"]
  if (typeof o.vip_coins === 'string') {
    o.vip_coins = o.vip_coins.split(',')
                             .map(s => s.trim().toUpperCase())
                             .filter(Boolean);
  }
  // vip_percentages: από textarea
  const pctArea  = form.elements['vip_percentages_text'];
  const prioArea = form.elements['vip_priority_list_text'];
  o.vip_percentages   = parseVipPercentages(pctArea ? pctArea.value : '');
  o.vip_priority_list = parseVipPriority(prioArea ? prioArea.value : '');

  return o;
}

function showMsg(text, type = 'ok') {
  const m = document.getElementById('controls-msg');
  m.textContent = text;
  m.className   = 'msg ' + type;
  setTimeout(() => { m.textContent = ''; m.className = 'msg'; }, 6000);
}

// -----------------------------------------------------------------
// Buttons
// -----------------------------------------------------------------

document.getElementById('btn-start').addEventListener('click', async () => {
  const cfg = getConfigFromForm();
  if (cfg.mode === 'live') {
    const ok = confirm(
      'ΠΡΟΣΟΧΗ: LIVE MODE\n\n' +
      'Θα σταλούν ΠΡΑΓΜΑΤΙΚΕΣ εντολές στο KuCoin.\n' +
      'Τα λεφτά σου είναι σε ρίσκο.\n\n' +
      'Είσαι σίγουρος;'
    );
    if (!ok) return;
  }
  try {
    const r = await api('/api/start', {method: 'POST', body: cfg});
    if (r.ok) {
      const resume = (r.resume && r.resume.decision) ? r.resume.decision : '?';
      showMsg(`Started ${r.mode} mode for ${r.symbol} (${resume})`, 'ok');
    } else {
      showMsg(`Start failed: ${r.error}`, 'error');
    }
    await loadStatus();
  } catch (e) { showMsg(`Start error: ${e}`, 'error'); }
});

document.getElementById('btn-stop').addEventListener('click', async () => {
  if (!confirm(
    'Soft Stop\n\n' +
    'Θα σταματήσει το bot. ΔΕΝ θα κλείσουν ανοιχτές margin θέσεις στο KuCoin.\n' +
    'Τυχόν ανοιχτές θέσεις θα πρέπει να τις χειριστείς χειροκίνητα.\n\n' +
    'Συνέχεια;'
  )) return;
  try {
    const r = await api('/api/stop', {method: 'POST'});
    if (r.ok) showMsg('Soft-stopped (no exchange actions)', 'ok');
    else       showMsg(`Stop failed: ${r.error}`, 'error');
    await loadStatus();
  } catch (e) { showMsg(`Stop error: ${e}`, 'error'); }
});

document.getElementById('btn-resset').addEventListener('click', async () => {
  if (!confirm(
    'RESSET_INVEST (Promote 3)\n\n' +
    'Θα κλείσει ΟΛΕΣ τις ανοιχτές θέσεις BASE_COIN.\n' +
    'Τα VIP_COINS ΔΕΝ θα αγγιχτούν.\n' +
    'Ο bot θα σταματήσει σε κατάσταση STOPPED.\n\n' +
    'Προχώρα;'
  )) return;
  try {
    const r = await api('/api/resset', {method: 'POST'});
    if (r.ok) showMsg(`Resset OK @ price ${r.price}`, 'ok');
    else       showMsg(`Resset failed: ${r.error}`, 'error');
    await loadStatus();
  } catch (e) { showMsg(`Resset error: ${e}`, 'error'); }
});

document.getElementById('btn-apply-live').addEventListener('click', async () => {
  const cfg = getConfigFromForm();
  try {
    const r = await api('/api/config/update', {method: 'POST', body: cfg});
    const parts = [];
    if (r.applied_live      && r.applied_live.length)      parts.push('LIVE: '      + r.applied_live.join(', '));
    if (r.queued_next_cycle && r.queued_next_cycle.length) parts.push('NEXT_CYCLE: ' + r.queued_next_cycle.join(', '));
    if (r.rejected_restart  && r.rejected_restart.length)  parts.push('RESTART skipped: ' + r.rejected_restart.join(', '));
    if (parts.length === 0) parts.push('No changes applicable');
    showMsg(parts.join(' | '), 'ok');
    await loadStatus();
  } catch (e) { showMsg(`Apply failed: ${e}`, 'error'); }
});

// -----------------------------------------------------------------
// Symbol preset + check button (A5)
// -----------------------------------------------------------------
document.getElementById('symbol-preset').addEventListener('change', (e) => {
  const v = e.target.value;
  if (!v) return;
  document.querySelector('[name="symbol"]').value = v;
});

document.getElementById('btn-check-symbol').addEventListener('click', async () => {
  const sym = document.querySelector('[name="symbol"]').value;
  const m = document.getElementById('symbol-check-msg');
  if (!sym) { m.textContent = 'Δώσε symbol πρώτα'; m.className = 'msg-inline error'; return; }
  m.textContent = 'Checking...'; m.className = 'msg-inline';
  try {
    const r = await api('/api/atr?timeframe=1h&symbol=' + encodeURIComponent(sym));
    if (r.error) {
      m.textContent = 'INVALID: ' + r.error;
      m.className = 'msg-inline error';
    } else {
      m.textContent = `OK — last close ${fmt(r.last_close, 10)}`;
      m.className = 'msg-inline ok';
    }
  } catch (e) {
    m.textContent = 'Check failed: ' + e;
    m.className = 'msg-inline error';
  }
});

// -----------------------------------------------------------------
// ATR on-demand (B3c)
// -----------------------------------------------------------------
document.getElementById('btn-atr').addEventListener('click', async () => {
  const tf = document.getElementById('atr-tf').value;
  const sym = document.querySelector('[name="symbol"]').value || 'PEPE/USDT';
  const out = document.getElementById('atr-result');
  out.textContent = 'Computing...';
  try {
    const r = await api(`/api/atr?symbol=${encodeURIComponent(sym)}&timeframe=${tf}`);
    if (r.error) {
      out.textContent = 'Error: ' + r.error;
    } else {
      out.innerHTML = `ATR(14, ${r.timeframe}) = <strong>${fmt(r.atr, 10)}</strong> (<strong>${r.atr_pct.toFixed(3)}%</strong>) · close ${fmt(r.last_close, 10)}`;
    }
  } catch (e) {
    out.textContent = 'Error: ' + e;
  }
});

// -----------------------------------------------------------------
// Tables
// -----------------------------------------------------------------
async function loadTrades() {
  const mode = (document.querySelector('[name="mode"]') || {}).value || 'paper';
  try {
    const trades = await api(`/api/trades?mode=${mode}`);
    const tbody = document.querySelector('#trades-table tbody');
    if (!Array.isArray(trades)) { tbody.innerHTML = '<tr><td colspan=8>No data</td></tr>'; return; }
    tbody.innerHTML = trades.map(t => `
      <tr>
        <td>${t.id}</td>
        <td>${(t.ts_iso||'').slice(0, 19)}</td>
        <td>${t.action}</td>
        <td>${t.symbol || ''}</td>
        <td>${fmt(t.quantity, 4)}</td>
        <td>${fmt(t.price, 10)}</td>
        <td>${fmt(t.usdt_value, 2)}</td>
        <td>${t.note || ''}</td>
      </tr>
    `).join('');
  } catch (e) { console.error('trades failed', e); }
}

async function loadStates() {
  const mode = (document.querySelector('[name="mode"]') || {}).value || 'paper';
  try {
    const events = await api(`/api/state?mode=${mode}`);
    const tbody = document.querySelector('#state-table tbody');
    if (!Array.isArray(events)) { tbody.innerHTML = '<tr><td colspan=3>No data</td></tr>'; return; }
    tbody.innerHTML = events.map(e => `
      <tr>
        <td>${(e.ts_iso||'').slice(0, 19)}</td>
        <td>${e.event}</td>
        <td>${e.state}</td>
      </tr>
    `).join('');
  } catch (e) { console.error('state failed', e); }
}

async function loadSymbols() {
  try {
    const items = await api('/api/symbols');
    const tbody = document.querySelector('#symbols-table tbody');
    if (!Array.isArray(items) || items.length === 0) {
      tbody.innerHTML = '<tr><td colspan=5>No trading history</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(it => `
      <tr>
        <td>${it.symbol}</td>
        <td>${it.mode}</td>
        <td>${it.trades}</td>
        <td>${it.cycles}</td>
        <td>${(it.last_activity||'').slice(0, 19)}</td>
      </tr>
    `).join('');
  } catch (e) { console.error('symbols failed', e); }
}

async function refreshAll() {
  await loadStatus();
  await loadTrades();
  await loadStates();
  await loadSymbols();
}

// Promote 2 event wiring
(function () {
  const promoteSel = document.querySelector('[name="promote"]');
  if (promoteSel) promoteSel.addEventListener('change', togglePromote2Section);

  const pctArea = document.querySelector('[name="vip_percentages_text"]');
  if (pctArea) pctArea.addEventListener('input', updateVipPercentSum);
})();


// --- Export trades CSV (manual backup, v5.4) ---
function downloadTradesCSV(mode) {
  // Δεν χρησιμοποιούμε api() γιατί θέλουμε binary download, όχι JSON.
  // Browser θα κατεβάσει το CSV ως αρχείο μέσω Content-Disposition header.
  window.location.href = `/api/trades/export?mode=${encodeURIComponent(mode)}`;
}
const _btnExportPaper = document.getElementById('btn-export-paper');
if (_btnExportPaper) _btnExportPaper.addEventListener('click', () => downloadTradesCSV('paper'));
const _btnExportLive  = document.getElementById('btn-export-live');
if (_btnExportLive)  _btnExportLive.addEventListener('click',  () => downloadT

// v6.x: Refresh VIP prices button handler
const btnRefreshVip = document.getElementById('btn-refresh-vip');
if (btnRefreshVip) {
  btnRefreshVip.addEventListener('click', async () => {
    btnRefreshVip.disabled = true;
    const origText = btnRefreshVip.textContent;
    btnRefreshVip.textContent = 'Refreshing...';
    try {
      const r = await api('/api/vip/refresh', {method: 'POST'});
      if (r.ok) {
        const summary = Object.entries(r.prices || {}).map(([c, p]) => `${c}=${p}`).join(', ');
        showMsg(`VIP prices refreshed: ${summary || '(no coins)'}`, 'ok');
      } else {
        showMsg(`VIP refresh failed: ${r.error}`, 'error');
      }
      await loadStatus();
    } catch (e) {
      showMsg(`VIP refresh error: ${e}`, 'error');
    } finally {
      btnRefreshVip.disabled = false;
      btnRefreshVip.textContent = origText;
    }
  });
}
