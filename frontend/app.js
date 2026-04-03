'use strict';

/* ── Config ─────────────────────────────────────────────────── */
const API_BASE = 'http://localhost:8031';
const WS_URL   = 'ws://localhost:8031/ws/alerts';

const RISK_COLOR = {
  LOW:      '#34d399',
  MEDIUM:   '#f59e0b',
  HIGH:     '#ef4444',
  CRITICAL: '#a78bfa',
};

/* ── Escape helper ─────────────────────────────────────────── */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ── State ─────────────────────────────────────────────────── */
let _latestEvent = null;
let _placements  = {};
let _zoneBehaviors = {};

/* ── Build the 16 heatmap cells once on load ────────────────── */
(function buildHeatmapCells() {
  const grid = document.getElementById('heatmap-grid');
  for (let i = 0; i < 16; i++) {
    const cell = document.createElement('div');
    cell.className = 'zone-cell';
    cell.id        = 'zone-' + i;
    cell.style.background  = 'rgba(52,211,153,0.06)';
    cell.style.borderColor = '#34d39922';
    cell.innerHTML = `
      <div class="quantum-overlay" id="qo-${i}"></div>
      <div class="zone-num">B${i + 1}</div>
      <div class="zone-risk" id="zr-${i}" style="color:#34d399">LOW</div>
      <div class="zone-behavior" id="zb-${i}"></div>
      <div class="zone-prob" id="zp-${i}"></div>
      <span class="zone-cam-icon" id="zc-${i}">▲</span>
      <div class="quantum-label" id="ql-${i}"></div>
    `;
    grid.appendChild(cell);
  }
})();

/* ── Heatmap update ─────────────────────────────────────────── */
function updateHeatmap(heatmap) {
  if (!Array.isArray(heatmap)) return;
  for (const h of heatmap) {
    const z      = h.zone_id;
    const cell   = document.getElementById('zone-' + z);
    const riskEl = document.getElementById('zr-' + z);
    if (!cell || !riskEl) continue;

    const risk  = h.risk  || 'LOW';
    const score = Math.min(1, h.score || 0);
    const color = RISK_COLOR[risk] || RISK_COLOR.LOW;

    let bg;
    if      (risk === 'HIGH')   bg = `rgba(239,68,68,${(0.2 + score * 0.7).toFixed(3)})`;
    else if (risk === 'MEDIUM') bg = `rgba(245,158,11,${(0.15 + score * 0.6).toFixed(3)})`;
    else                        bg = `rgba(52,211,153,${(0.06 + score * 0.15).toFixed(3)})`;

    cell.style.background  = bg;
    cell.style.borderColor = color + '44';
    if (risk === 'HIGH') cell.style.boxShadow = '0 0 12px rgba(239,68,68,0.2)';
    else cell.style.boxShadow = 'none';
    riskEl.textContent     = risk;
    riskEl.style.color     = color;
  }
}

/* ── Zone behavior labels on heatmap ────────────────────────── */
function updateZoneBehaviors(evt) {
  if (!evt) return;
  const zoneId = evt.zone_id;
  const risk = evt.risk_tier || 'LOW';
  const behavior = evt.behavior || '';
  const traj = evt.trajectory || {};

  if (risk !== 'LOW' && behavior) {
    let label = behavior.replace(/_/g, ' ');
    if (traj.is_suspicious) label = traj.label.toLowerCase();
    _zoneBehaviors[zoneId] = label;
  }

  for (const [z, lbl] of Object.entries(_zoneBehaviors)) {
    const el = document.getElementById('zb-' + z);
    if (el) el.textContent = lbl;
  }
}

/* ── Quantum probability overlay ────────────────────────────── */
function updateQuantum(quantum) {
  if (!quantum || !Array.isArray(quantum.field)) return;
  for (const q of quantum.field) {
    const probEl = document.getElementById('zp-' + q.zone_id);
    if (!probEl) continue;
    if (q.probability > 0.01) {
      probEl.textContent = 'ψ ' + (q.probability * 100).toFixed(1) + '%';
      probEl.classList.add('show');
    } else {
      probEl.textContent = '';
      probEl.classList.remove('show');
    }
  }
}

/* ── Quantum overlay (flat quantum_field array) ────────────── */
function updateQuantumOverlay(quantumField, quantumState, quantumEntropy) {
  if (!quantumField) return;
  quantumField.forEach(({ zone_id, probability }) => {
    const overlay = document.getElementById(`qo-${zone_id}`);
    const label   = document.getElementById(`ql-${zone_id}`);
    if (!overlay || !label) return;

    const alpha = Math.min(0.75, probability * 0.9);
    overlay.style.background = `rgba(167, 139, 250, ${alpha})`;

    if (probability > 0.05) {
      overlay.classList.add('quantum-active');
      label.textContent = `ψ ${probability.toFixed(2)}`;
    } else {
      overlay.classList.remove('quantum-active');
      label.textContent = '';
    }
  });

  const badge = document.getElementById('quantum-state-badge');
  if (badge) {
    const colors = {
      tracking:  '#34d399',
      diffusing: '#a78bfa',
      collapsed: '#f59e0b',
      idle:      '#444',
    };
    badge.textContent = `ψ ${quantumState || 'idle'} · H=${(quantumEntropy || 0).toFixed(2)}`;
    badge.style.color = colors[quantumState] || '#666';
  }
}

/* ── Camera placement ▲ markers ─────────────────────────────── */
function applyPlacements(data) {
  if (!data || !Array.isArray(data.block_assignments)) return;
  _placements = {};
  for (const b of data.block_assignments) {
    _placements[b.zone_id] = true;
    const el = document.getElementById('zc-' + b.zone_id);
    if (el) el.style.display = 'block';
  }
}

/* ── Feature pill pulse logic ──────────────────────────────── */
const PILL_TIMEOUT = {};
function updateFeaturePills(evt) {
  const checks = [
    { id: 'pill-flow',       cls: 'pulse-flow',       active: (evt.flow_magnitude || 0) > 2.0 },
    { id: 'pill-divcurl',    cls: 'pulse-divcurl',    active: Math.abs(evt.divergence || 0) > 0.3 || Math.abs(evt.curl || 0) > 0.3 },
    { id: 'pill-lyapunov',   cls: 'pulse-lyapunov',   active: (evt.lyapunov || 0) > 0 },
    { id: 'pill-quantum',    cls: 'pulse-quantum',    active: (evt.quantum_state || '') === 'diffusing' },
    { id: 'pill-clip',       cls: 'pulse-clip',       active: (evt.clip_score || 0) > 0.4 },
    { id: 'pill-trajectory', cls: 'pulse-trajectory', active: !!(evt.trajectory && evt.trajectory.is_suspicious) },
  ];

  for (const { id, cls, active } of checks) {
    const el = document.getElementById(id);
    if (!el) continue;
    if (active) {
      el.classList.add(cls);
      clearTimeout(PILL_TIMEOUT[id]);
      PILL_TIMEOUT[id] = setTimeout(() => el.classList.remove(cls), 4000);
    }
  }
}

/* ── Trajectory panel update ────────────────────────────────── */
function updateTrajectory(traj) {
  if (!traj) return;

  // Path entropy
  const entropy = traj.path_entropy || 0;
  const entBar = document.getElementById('traj-entropy-bar');
  const entVal = document.getElementById('traj-entropy-val');
  const entSt  = document.getElementById('traj-entropy-status');
  if (entBar) entBar.style.width = Math.min(100, (entropy / 3.5) * 100) + '%';
  if (entBar) entBar.style.background = entropy > 2.0 ? '#ef4444' : entropy < 1.2 ? '#34d399' : '#f59e0b';
  if (entVal) entVal.textContent = entropy.toFixed(2);
  if (entSt) {
    if (entropy > 2.0) { entSt.textContent = 'SUSPICIOUS'; entSt.style.background = '#3d1010'; entSt.style.color = '#fca5a5'; }
    else if (entropy < 1.2) { entSt.textContent = 'NORMAL'; entSt.style.background = '#0e1e12'; entSt.style.color = '#34d399'; }
    else { entSt.textContent = 'MODERATE'; entSt.style.background = '#3d2f10'; entSt.style.color = '#fcd34d'; }
  }

  // Efficiency
  const eff = traj.displacement_efficiency || 0;
  const effBar = document.getElementById('traj-eff-bar');
  const effVal = document.getElementById('traj-eff-val');
  const effSt  = document.getElementById('traj-eff-status');
  if (effBar) effBar.style.width = Math.min(100, eff * 100) + '%';
  if (effBar) effBar.style.background = eff < 0.35 ? '#ef4444' : eff > 0.6 ? '#34d399' : '#f59e0b';
  if (effVal) effVal.textContent = eff.toFixed(2);
  if (effSt) {
    if (eff < 0.35) { effSt.textContent = 'MULE'; effSt.style.background = '#3d1010'; effSt.style.color = '#fca5a5'; }
    else if (eff > 0.6) { effSt.textContent = 'NORMAL'; effSt.style.background = '#0e1e12'; effSt.style.color = '#34d399'; }
    else { effSt.textContent = 'MODERATE'; effSt.style.background = '#3d2f10'; effSt.style.color = '#fcd34d'; }
  }

  // Oscillations
  const osc = traj.oscillation_count || 0;
  const oscBar = document.getElementById('traj-osc-bar');
  const oscVal = document.getElementById('traj-osc-val');
  const oscSt  = document.getElementById('traj-osc-status');
  if (oscBar) oscBar.style.width = Math.min(100, (osc / 8) * 100) + '%';
  if (oscBar) oscBar.style.background = osc > 3 ? '#ef4444' : osc <= 1 ? '#34d399' : '#f59e0b';
  if (oscVal) oscVal.textContent = osc;
  if (oscSt) {
    if (osc > 3) { oscSt.textContent = 'ZIGZAG'; oscSt.style.background = '#3d1010'; oscSt.style.color = '#fca5a5'; }
    else if (osc <= 1) { oscSt.textContent = 'NORMAL'; oscSt.style.background = '#0e1e12'; oscSt.style.color = '#34d399'; }
    else { oscSt.textContent = 'MODERATE'; oscSt.style.background = '#3d2f10'; oscSt.style.color = '#fcd34d'; }
  }

  // Label pill
  const labelPill = document.getElementById('traj-label-pill');
  if (labelPill) {
    const suspicious = traj.is_suspicious;
    labelPill.textContent = suspicious ? traj.label + ' detected' : 'Normal path';
    labelPill.style.background = suspicious ? '#3d1010' : '#0e1e12';
    labelPill.style.color      = suspicious ? '#fca5a5' : '#34d399';
  }
}

/* ── Physics panel update ──────────────────────────────────── */
function updatePhysics(evt) {
  const div  = evt.divergence || 0;
  const curl = evt.curl || 0;
  const lyap = evt.lyapunov || 0;
  const flow = evt.flow_magnitude || 0;

  const divEl   = document.getElementById('phys-div');
  const curlEl  = document.getElementById('phys-curl');
  const lyapEl  = document.getElementById('phys-lyap');
  const flowEl  = document.getElementById('phys-flow');
  const divTag  = document.getElementById('phys-div-tag');
  const curlTag = document.getElementById('phys-curl-tag');
  const lyapTag = document.getElementById('phys-lyap-tag');
  const flowTag = document.getElementById('phys-flow-tag');

  if (divEl) {
    divEl.textContent = div.toFixed(4);
    divEl.style.color = Math.abs(div) > 0.3 ? '#fca5a5' : '#888';
  }
  if (curlEl) {
    curlEl.textContent = curl.toFixed(4);
    curlEl.style.color = Math.abs(curl) > 0.3 ? '#fca5a5' : '#888';
  }
  if (lyapEl) {
    lyapEl.textContent = lyap.toFixed(4);
    lyapEl.style.color = lyap > 0 ? '#fca5a5' : '#34d399';
  }
  if (flowEl) {
    flowEl.textContent = flow.toFixed(3);
    flowEl.style.color = flow > 2.0 ? '#c4b5fd' : '#888';
  }

  if (divTag) {
    divTag.textContent = Math.abs(div) > 0.5 ? 'SCATTER' : Math.abs(div) > 0.3 ? 'ANOMALY' : 'normal';
    divTag.style.color = Math.abs(div) > 0.3 ? '#fca5a5' : '#555';
  }
  if (curlTag) {
    curlTag.textContent = Math.abs(curl) > 0.5 ? 'FIGHT' : Math.abs(curl) > 0.3 ? 'ANOMALY' : 'normal';
    curlTag.style.color = Math.abs(curl) > 0.3 ? '#fca5a5' : '#555';
  }
  if (lyapTag) {
    lyapTag.textContent = lyap > 0 ? 'CHAOTIC' : 'stable';
    lyapTag.style.color = lyap > 0 ? '#fca5a5' : '#555';
  }
  if (flowTag) {
    flowTag.textContent = flow > 4.5 ? 'FAST' : flow > 2.0 ? 'MOTION' : 'quiet';
    flowTag.style.color = flow > 2.0 ? '#c4b5fd' : '#555';
  }
}

/* ── Alert card renderer — split layout ──────────────────── */
function renderAlertCard(evt) {
  const r     = evt.reasoning || {};
  const risk  = r.risk_level || evt.risk_tier || 'LOW';
  const color = RISK_COLOR[risk] || RISK_COLOR.LOW;
  const ts    = evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : '';
  const traj  = evt.trajectory || {};

  let whyHtml = '';
  if (Array.isArray(r.why_flagged) && r.why_flagged.length) {
    const items = r.why_flagged.map(w => `<div class="why-item">${esc(w)}</div>`).join('');
    whyHtml = `<div class="why-list">
                 <span class="section-label">Why flagged:</span>
                 ${items}
               </div>`;
  }

  const patHtml = evt.pattern_id
    ? `<p class="alert-pattern">Pattern ${esc(evt.pattern_id)} · ${esc(evt.recurrence || 0)} prior occurrences</p>`
    : '';

  const predHtml = r.predicted_next
    ? `<div class="predicted-box"><span class="label">Predicted: </span>${esc(r.predicted_next)}</div>`
    : '';

  const actHtml = r.recommended_action
    ? `<div class="action-box"><span class="label">Action: </span>${esc(r.recommended_action)}</div>`
    : '';

  // Build physics summary line
  const phys = [];
  if (evt.lyapunov > 0) phys.push(`λ>${esc(evt.lyapunov.toFixed(2))}`);
  if (Math.abs(evt.curl || 0) > 0.3) phys.push(`∇×F=${esc(Math.abs(evt.curl).toFixed(2))}`);
  if (Math.abs(evt.divergence || 0) > 0.3) phys.push(`∇·F=${esc(Math.abs(evt.divergence).toFixed(2))}`);
  if ((evt.quantum_state || '') === 'diffusing') phys.push('ψ diffusing');
  const physLine = phys.length > 0 ? phys.join(' · ') : 'No physics anomalies';

  // Recurrence label
  const rec = evt.recurrence || 0;
  const recLabel = rec > 0 ? `${rec}${rec===1?'st':rec===2?'nd':rec===3?'rd':'th'} time` : 'First seen';

  // Trajectory label
  const trajHtml = traj.is_suspicious
    ? `<div class="meta-line"><span class="meta-badge" style="background:#3d1030;color:#f9a8d4;">🗺 ${esc(traj.label)}</span></div>`
    : '';

  document.getElementById('alert-card').innerHTML = `
    <div class="alert-split">
      <div class="alert-left">
        <div class="alert-header">
          <span class="risk-pill" style="background:${color}">${esc(risk)}</span>
          <span class="alert-zone-tag">Zone ${esc(evt.zone_id)}</span>
          <span class="alert-time-tag">${esc(ts)}</span>
        </div>
        <p class="alert-summary">${esc(r.pattern_summary || evt.behavior_label || '')}</p>
        ${patHtml}
        ${whyHtml}
        ${predHtml}
        ${actHtml}
      </div>
      <div class="alert-right">
        <div class="risk-badge-large" style="background:${color}">${esc(risk)}</div>
        <div class="meta-line">📊 ${esc(recLabel)}</div>
        ${evt.pattern_id ? `<div class="meta-line"><span class="meta-badge" style="background:#2d1f4e;color:#c4b5fd;">${esc(evt.pattern_id)}</span></div>` : ''}
        ${trajHtml}
        <div class="physics-summary">${physLine}</div>
      </div>
    </div>
  `;
}

/* ── Event feed row ─────────────────────────────────────────── */
let _feedCount = 0;
const MAX_FEED = 100;

function addEventRow(evt) {
  const placeholder = document.getElementById('feed-empty');
  if (placeholder) placeholder.remove();

  const risk  = evt.risk_tier || 'LOW';
  const color = RISK_COLOR[risk] || RISK_COLOR.LOW;
  const ts    = evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : '';

  const row = document.createElement('div');
  row.className = 'event-row';
  row.innerHTML = `
    <span class="evt-dot" style="background:${color}"></span>
    <span class="evt-time">${esc(ts)}</span>
    <span class="evt-zone">Z${esc(evt.zone_id)}</span>
    <span class="evt-label">${esc(evt.behavior_label || evt.behavior || '')}</span>
    ${evt.pattern_id ? `<span class="evt-pat">${esc(evt.pattern_id)}</span>` : ''}
    <span class="evt-risk" style="color:${color}">${esc(risk)}</span>
    ${evt.pqc_signature ? `<span class="evt-pqc" title="PQC Signed">🔐</span>` : ''}
  `;

  const feed = document.getElementById('event-feed');
  feed.insertBefore(row, feed.firstChild);

  _feedCount++;
  if (_feedCount > MAX_FEED) {
    feed.removeChild(feed.lastChild);
    _feedCount = MAX_FEED;
  }
}

/* ── System state badge (neuromorphic sleep / active) ───────── */
function updateSystemBadge(isSleeping) {
  const dot = document.getElementById('system-dot');
  const lbl = document.getElementById('system-label');
  if (dot) dot.style.background = isSleeping ? '#f59e0b' : '#34d399';
  if (lbl) lbl.textContent = 'Neuromorphic — ' + (isSleeping ? 'sleep' : 'active');
}

/* ── Stats poller with count-up animation ──────────────────── */
const _prevStats = {};
function animateStat(id, newVal, isFloat) {
  const el = document.getElementById(id);
  if (!el) return;
  const displayVal = isFloat ? parseFloat(newVal).toFixed(2) : newVal;
  if (_prevStats[id] !== displayVal) {
    el.textContent = displayVal;
    el.classList.add('bump');
    setTimeout(() => el.classList.remove('bump'), 300);
    _prevStats[id] = displayVal;
  }
}

function formatUptime(s) {
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm';
  return Math.floor(s/3600) + 'h ' + Math.floor((s%3600)/60) + 'm';
}

function pollStats() {
  fetch(API_BASE + '/stats')
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => {
      animateStat('stat-total',    d.total_incidents ?? 0);
      animateStat('stat-memory',   d.memory_events   ?? 0);
      animateStat('stat-zones',    d.active_zones    ?? 0);
      animateStat('stat-clip',     d.avg_clip_score  ?? 0, true);
      animateStat('stat-patterns', d.patterns_found  ?? 0);
      const el = document.getElementById('stat-uptime');
      if (el) {
        const txt = formatUptime(d.uptime_s ?? 0);
        if (_prevStats['stat-uptime'] !== txt) {
          el.textContent = txt;
          _prevStats['stat-uptime'] = txt;
        }
      }
    })
    .catch(() => {});
}

/* ── Connection quality — latency measurement ───────────────── */
let _wsConnected = false;
let _wsAttempts  = 0;
let _latencyMs   = null;

function measureLatency() {
  const t0 = performance.now();
  fetch(API_BASE + '/health')
    .then(r => {
      if (r.ok) {
        _latencyMs = Math.round(performance.now() - t0);
        updateConnectionBadge();
      }
      return r.json();
    })
    .catch(() => {
      _latencyMs = null;
      updateConnectionBadge();
    });
}

function updateConnectionBadge() {
  const dot  = document.getElementById('status-dot');
  const text = document.getElementById('status-text');
  if (!dot || !text) return;

  if (_wsConnected) {
    dot.style.background = '#34d399';
    text.textContent = _latencyMs != null ? `Live · ${_latencyMs}ms` : 'Live';
    text.style.color = '#34d399';
  } else if (_wsAttempts > 0) {
    dot.style.background = '#f59e0b';
    text.textContent = `Reconnecting (${_wsAttempts})...`;
    text.style.color = '#f59e0b';
  } else {
    dot.style.background = '#ef4444';
    text.textContent = 'Offline';
    text.style.color = '#ef4444';
  }
}

/* ── Handle system reset message ────────────────────────────── */
function handleSystemReset() {
  // Clear heatmap
  for (let i = 0; i < 16; i++) {
    const cell = document.getElementById('zone-' + i);
    const risk = document.getElementById('zr-' + i);
    const beh  = document.getElementById('zb-' + i);
    if (cell) { cell.style.background = 'rgba(52,211,153,0.06)'; cell.style.borderColor = '#34d39922'; cell.style.boxShadow = 'none'; }
    if (risk) { risk.textContent = 'LOW'; risk.style.color = '#34d399'; }
    if (beh)  { beh.textContent = ''; }
  }
  _zoneBehaviors = {};
  // Clear feed
  const feed = document.getElementById('event-feed');
  feed.innerHTML = '<p class="feed-empty" id="feed-empty">No events yet</p>';
  _feedCount = 0;
  // Clear alert card
  document.getElementById('alert-card').innerHTML = '<p class="alert-waiting">Waiting for events...</p>';
  // Clear trajectory
  ['traj-entropy-bar','traj-eff-bar','traj-osc-bar'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.width = '0';
  });
  ['traj-entropy-val','traj-eff-val','traj-osc-val'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = '—';
  });
  const lp = document.getElementById('traj-label-pill');
  if (lp) { lp.textContent = 'Normal path'; lp.style.background = '#0e1e12'; lp.style.color = '#34d399'; }
}

/* ── Demo controls ─────────────────────────────────────────── */
function toggleDemoPanel() {
  const body = document.getElementById('demo-body');
  const toggle = document.getElementById('demo-toggle');
  body.classList.toggle('collapsed');
  toggle.textContent = body.classList.contains('collapsed') ? '▶' : '▼';
}

function simulateHighRisk() {
  const btn = document.getElementById('simulate-btn');
  btn.textContent = '⏳ Simulating...';
  btn.disabled = true;
  fetch(API_BASE + '/demo/simulate', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({risk: 'HIGH', behavior: 'fast_movement'})
  })
  .then(r => r.json())
  .then(d => {
    btn.textContent = '✅ Sent! Zone ' + d.zone_id;
    setTimeout(() => { btn.textContent = '⚡ Simulate HIGH Risk Event'; btn.disabled = false; }, 2000);
  })
  .catch(() => {
    btn.textContent = '❌ Failed';
    setTimeout(() => { btn.textContent = '⚡ Simulate HIGH Risk Event'; btn.disabled = false; }, 2000);
  });
}

function runTamperDemo() {
  const btn = document.getElementById('tamper-btn');
  btn.textContent = '⏳ Running...';
  btn.disabled = true;
  const result = document.getElementById('tamper-result');
  fetch(API_BASE + '/demo/tamper', { method: 'POST' })
    .then(r => r.json())
    .then(d => {
      result.innerHTML =
        `<span style="color:#34d399">✅ Original: ${d.original ? 'VALID' : 'INVALID'}</span> ` +
        `<span style="color:#ef4444">❌ Tampered: ${d.tampered ? 'VALID' : 'REJECTED'}</span> ` +
        `<span style="color:#555">Sig: ${esc(d.signature)}</span>`;
      btn.textContent = '🔐 Run Tamper Demo';
      btn.disabled = false;
    })
    .catch(() => {
      result.innerHTML = '<span style="color:#ef4444">❌ Backend unreachable</span>';
      btn.textContent = '🔐 Run Tamper Demo';
      btn.disabled = false;
    });
}

function resetAllZones() {
  const btn = document.getElementById('reset-btn');
  btn.textContent = '⏳ Resetting...';
  btn.disabled = true;
  fetch(API_BASE + '/demo/reset', { method: 'POST' })
    .then(r => r.json())
    .then(() => {
      btn.textContent = '✅ Reset!';
      handleSystemReset();
      setTimeout(() => { btn.textContent = '🔄 Reset All Zones'; btn.disabled = false; }, 2000);
    })
    .catch(() => {
      btn.textContent = '❌ Failed';
      setTimeout(() => { btn.textContent = '🔄 Reset All Zones'; btn.disabled = false; }, 2000);
    });
}

/* ── Load initial data ───────────────────────────────────────── */
function loadInitialData() {
  fetch(API_BASE + '/events')
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(evts => {
      if (!Array.isArray(evts) || evts.length === 0) return;
      [...evts].reverse().forEach(addEventRow);
      const latest = evts[0];
      _latestEvent = latest;
      if (Array.isArray(latest.heatmap)) updateHeatmap(latest.heatmap);
      if (latest.quantum)                updateQuantum(latest.quantum);
      if (latest.quantum_field)          updateQuantumOverlay(latest.quantum_field, latest.quantum_state, latest.quantum_entropy);
      renderAlertCard(latest);
      updatePhysics(latest);
      updateTrajectory(latest.trajectory);
      updateFeaturePills(latest);
      updateZoneBehaviors(latest);
    })
    .catch(() => {});

  fetch(API_BASE + '/aqhso/placements')
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(applyPlacements)
    .catch(() => {});

  pollStats();
}

/* ── WebSocket with automatic reconnect ──────────────────────── */
let _wsRetryMs = 2000;

function connect() {
  const ws = new WebSocket(WS_URL);

  ws.onopen = () => {
    _wsRetryMs   = 2000;
    _wsConnected = true;
    _wsAttempts  = 0;
    updateConnectionBadge();
    measureLatency();
  };

  ws.onclose = () => {
    _wsConnected = false;
    _wsAttempts++;
    updateConnectionBadge();
    setTimeout(connect, Math.min(_wsRetryMs, 10000));
    _wsRetryMs = Math.min(_wsRetryMs * 2, 10000);
  };

  ws.onerror = () => ws.close();

  ws.onmessage = (e) => {
    let evt;
    try { evt = JSON.parse(e.data); } catch { return; }

    if (evt.type === 'system_state') {
      updateSystemBadge(evt.sleep_mode);
      return;
    }

    if (evt.type === 'system_reset') {
      handleSystemReset();
      return;
    }

    _latestEvent = evt;
    addEventRow(evt);
    if (Array.isArray(evt.heatmap)) updateHeatmap(evt.heatmap);
    if (evt.quantum)                updateQuantum(evt.quantum);
    if (evt.quantum_field)          updateQuantumOverlay(evt.quantum_field, evt.quantum_state, evt.quantum_entropy);
    renderAlertCard(evt);
    updatePhysics(evt);
    updateTrajectory(evt.trajectory);
    updateFeaturePills(evt);
    updateZoneBehaviors(evt);
  };
}

/* ── Boot ────────────────────────────────────────────────────── */
loadInitialData();
connect();
setInterval(pollStats, 3000);
setInterval(measureLatency, 5000);
