// Arbitrading Bot Dashboard - Client JS

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

function renderStatus(s) {
  const running = !!s.running;
  const badge = document.getElementById('status-badge');
  badge.textContent = running ? 'RUNNING' : 'STOPPED';
  badge.className = 'badge ' + (running ? 'badge-running' : 'badge-idle');
  if (s.last_error) {
    badge.textContent = 'ERROR';
    badge.className = 'badge badge-error';
    badge.title = s.last_error;
  }

  document.getElementById('btn-start').disabled  = running;
  document.getElementById('btn-stop').disabled   = !running;
  document.getElementById('btn-resset').disabled = !running;

  setText('m-mode',   s.mode || '-');
  setText('m-symbol', s.symbol || '-');
  setText('m-ticks',  s.tick_count || 0);

  const st = s.strategy || {};
  const sn = s.snapshot || {};
  setText('m-state',   st.state || '-');
  setText('m-cycles',  st.cycle_count || 0);
  setText('m-price',   (s.feed && s.feed.last_price !== null && s.feed.last_price !== undefined) ? fmt(s.feed.last_price, 10) : '-');
  setText('m-ref',     fmt(st.reference_price, 10));
  setText('m-ratio',   fmt(sn.margin_ratio, 4));
  setText('m-buy-cnt', st.buy_trigger_count || 0);
  setText('m-sell-cnt',st.sell_trigger_count || 0);
  setText('m-hb',      st.has_bought === true ? 'true' : (st.has_bought === false ? 'false' : '-'));
  setText('m-apb',     st.active_profit_pct_buy !== undefined ? fmt(st.active_profit_pct_buy, 2) : '-');

  setText('b-base',    fmt(sn.base_coin, 4));
  setText('b-bborrow', fmt(sn.base_debt, 4));
  setText('b-usdt',    fmt(sn.usdt, 2));
  setText('b-udebt',   fmt(sn.usdt_debt, 2));
  setText('b-vdebt',   fmt(sn.vip_debt_usdt, 2));
  setText('b-vip',     JSON.stringify(sn.vip_holdings || {}));
  setText('b-grand',   fmt(st.grand_amount, 2));
  setText('b-tassets', fmt(sn.total_assets, 2));
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
      if (el.type === 'checkbox') el.checked = !!v;
      else el.value = (typeof v === 'boolean') ? String(v) : v;
    }
  } catch (e) {
    console.error('config load failed', e);
  }
}

function getConfigFromForm() {
  const form = document.getElementById('cfg-form');
  const fd = new FormData(form);
  const o = {};
  for (const [k, v] of fd.entries()) {
    o[k] = v;
  }
  // Types
  o.second_profit_enabled = (o.second_profit_enabled === 'true');
  for (const key of ['start_base_coin','scale_base_coin','min_profit_percent','step_point',
                     'trailing_stop','margin_level','second_profit_percent','poll_interval']) {
    if (o[key] !== undefined && o[key] !== '') o[key] = parseFloat(o[key]);
  }
  o.promote = parseInt(o.promote || '1', 10);
  return o;
}

function showMsg(text, type = 'ok') {
  const m = document.getElementById('controls-msg');
  m.textContent = text;
  m.className   = 'msg ' + type;
  setTimeout(() => { m.textContent = ''; m.className = 'msg'; }, 5000);
}

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
    if (r.ok) showMsg(`Started ${r.mode} mode for ${r.symbol}`, 'ok');
    else       showMsg(`Start failed: ${r.error}`, 'error');
    await loadStatus();
  } catch (e) { showMsg(`Start error: ${e}`, 'error'); }
});

document.getElementById('btn-stop').addEventListener('click', async () => {
  if (!confirm('Stop bot?')) return;
  try {
    const r = await api('/api/stop', {method: 'POST'});
    if (r.ok) showMsg('Stopped', 'ok');
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

async function refreshAll() {
  await loadStatus();
  await loadTrades();
  await loadStates();
}

// Init
loadConfig().then(refreshAll);
setInterval(refreshAll, REFRESH_MS);
