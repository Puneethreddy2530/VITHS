'use strict';

/* ── Config ─────────────────────────────────────────────────── */
const API_BASE = 'http://localhost:8000';
const WS_URL   = 'ws://localhost:8000/ws/alerts';

const RISK_COLOR = {
  LOW:      '#1D9E75',
  MEDIUM:   '#EF9F27',
  HIGH:     '#E24B4A',
  CRITICAL: '#7F77DD',
};

/* ── Escape helper (prevent XSS in innerHTML writes) ─────────── */
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ── Build the 16 heatmap cells once on load ────────────────── */
(function buildHeatmapCells() {
  const grid = document.getElementById('heatmap-grid');
  for (let i = 0; i < 16; i++) {
    const cell = document.createElement('div');
    cell.className = 'zone-cell';
    cell.id        = 'zone-' + i;
    cell.style.background  = 'rgba(29,158,117,0.08)';
    cell.style.borderColor = '#1D9E7544';
    cell.innerHTML = `
      <div class="quantum-overlay" id="qo-${i}"></div>
      <div class="zone-num">B${i + 1}</div>
      <div class="zone-risk" id="zr-${i}" style="color:#1D9E75">LOW</div>
      <div class="zone-prob" id="zp-${i}"></div>
      <span class="zone-cam-icon" id="zc-${i}">▲</span>
      <div class="quantum-label" id="ql-${i}"></div>
    `;
    grid.appendChild(cell);
  }
})();

/* ── Heatmap update — pure DOM, no re-render ────────────────── */
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
    if      (risk === 'HIGH')   bg = `rgba(226,75,74,${(0.2 + score * 0.7).toFixed(3)})`;
    else if (risk === 'MEDIUM') bg = `rgba(239,159,39,${(0.2 + score * 0.6).toFixed(3)})`;
    else                        bg = `rgba(29,158,117,${(0.08 + score * 0.2).toFixed(3)})`;

    cell.style.background  = bg;
    cell.style.borderColor = color + '44';
    riskEl.textContent     = risk;
    riskEl.style.color     = color;
  }
}

/* ── Quantum probability overlay on heatmap cells ───────────── */
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

/* ── Quantum probability overlay (flat quantum_field array) ─── */
function updateQuantumOverlay(quantumField, quantumState, quantumEntropy) {
  if (!quantumField) return;
  quantumField.forEach(({ zone_id, probability }) => {
    const overlay = document.getElementById(`qo-${zone_id}`);
    const label   = document.getElementById(`ql-${zone_id}`);
    if (!overlay || !label) return;

    const alpha = Math.min(0.75, probability * 0.9);
    overlay.style.background = `rgba(127, 119, 221, ${alpha})`;

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
      tracking:  '#1D9E75',
      diffusing: '#7F77DD',
      collapsed: '#EF9F27',
      idle:      '#444',
    };
    badge.textContent = `ψ ${quantumState || 'idle'} · H=${(quantumEntropy || 0).toFixed(2)}`;
    badge.style.color = colors[quantumState] || '#888';
  }
}

/* ── Camera placement ▲ markers ─────────────────────────────── */
function applyPlacements(data) {
  if (!data || !Array.isArray(data.block_assignments)) return;
  for (const b of data.block_assignments) {
    const el = document.getElementById('zc-' + b.zone_id);
    if (el) el.style.display = 'block';
  }
}

/* ── Alert card renderer ─────────────────────────────────────── */
function renderAlertCard(evt) {
  const r     = evt.reasoning || {};
  const risk  = r.risk_level || evt.risk_tier || 'LOW';
  const color = RISK_COLOR[risk] || RISK_COLOR.LOW;
  const ts    = evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : '';

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

  document.getElementById('alert-card').innerHTML = `
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

/* ── System state badge (neuromorphic sleep / active) ──────────
   Triggered by evt.type === "system_state" on the WS channel —
   a separate message type from regular alert events.            */
function updateSystemBadge(isSleeping) {
  document.getElementById('status-dot').style.background =
    isSleeping ? '#EF9F27' : '#1D9E75';
  document.getElementById('status-text').textContent =
    'System: ' + (isSleeping ? 'Idle' : 'Active');
}

/* ── Stats poller (/stats every 5 s) ─────────────────────────── */
function pollStats() {
  fetch(API_BASE + '/stats')
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(d => {
      document.getElementById('stat-total').textContent  = d.total_incidents ?? 0;
      document.getElementById('stat-memory').textContent = d.memory_events   ?? 0;
    })
    .catch(() => {});
}

/* ── Load initial data on page open ──────────────────────────── */
function loadInitialData() {
  fetch(API_BASE + '/events')
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(evts => {
      if (!Array.isArray(evts) || evts.length === 0) return;
      [...evts].reverse().forEach(addEventRow);
      const latest = evts[0];
      if (Array.isArray(latest.heatmap)) updateHeatmap(latest.heatmap);
      if (latest.quantum)                updateQuantum(latest.quantum);
      renderAlertCard(latest);
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
    _wsRetryMs = 2000;
    document.getElementById('status-dot').style.background = '#1D9E75';
    document.getElementById('status-text').textContent = 'System: Active';
  };

  ws.onclose = () => {
    document.getElementById('status-dot').style.background = '#E24B4A';
    document.getElementById('status-text').textContent = 'Connecting...';
    setTimeout(connect, Math.min(_wsRetryMs, 10000));
    _wsRetryMs = Math.min(_wsRetryMs * 2, 10000);
  };

  ws.onerror = () => ws.close();

  ws.onmessage = (e) => {
    let evt;
    try { evt = JSON.parse(e.data); } catch { return; }

    /* Two distinct message types on the same socket:
       1. system_state  → neuromorphic sleep/wake badge only
       2. alert event   → feed row + heatmap + alert card      */
    if (evt.type === 'system_state') {
      updateSystemBadge(evt.sleep_mode);
      return;
    }

    addEventRow(evt);
    if (Array.isArray(evt.heatmap)) updateHeatmap(evt.heatmap);
    if (evt.quantum)                updateQuantum(evt.quantum);
    renderAlertCard(evt);
  };
}

/* ── Boot ────────────────────────────────────────────────────── */
loadInitialData();
connect();
setInterval(pollStats, 5000);
