'use strict';

/* ── Config ─────────────────────────────────────────────────── */
const API_BASE = '';
const WS_URL   = `ws://${window.location.host}/ws/alerts`;

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
let currentFloor = 1; // Default to Ground / Floor 1
const TOTAL_FLOORS = 16;

/**
 * Map F*_Z* suffix to SVG zone index 0..15.
 * Z1..Z16 = blocks B1..Gate (1-based, matches mobile zoneNumber = backend+1).
 * Z0..Z15 = legacy 0-based if ever sent.
 */
function zoneIndexFromZNotation(zRaw) {
  const z = typeof zRaw === 'number' ? zRaw : parseInt(String(zRaw).replace(/^Z/i, ''), 10);
  if (!Number.isFinite(z)) return null;
  if (z >= 1 && z <= 16) return z - 1;
  if (z >= 0 && z <= 15) return z;
  return null;
}

/** Parse backend zone ids: F3_Z5 → floor 3, B5 (index 4); plain 5 → floor 1, backend index 5 (B6) */
function parseFloorZone(raw) {
  if (raw == null) return { floor: currentFloor, localZone: null };
  const s = String(raw).trim();
  if (!s.includes('_')) {
    const n = parseInt(s, 10);
    return { floor: 1, localZone: Number.isFinite(n) && n >= 0 && n <= 15 ? n : null };
  }
  const parts = s.split('_');
  const floorStr = parts[0].replace(/^F/i, '');
  const parsedFloor = parseInt(floorStr, 10);
  const zPart = parts[1] || '';
  const localZone = zoneIndexFromZNotation(zPart);
  return {
    floor: Number.isFinite(parsedFloor) ? parsedFloor : 1,
    localZone,
  };
}

function clearAllFloorThreats() {
  document.querySelectorAll('.floor-btn').forEach(b => b.classList.remove('threat'));
}

/** Clear the 2D map layers so another floor does not inherit the previous floor's zone scores. */
function resetPerFloorZoneOverlays() {
  for (let i = 0; i < 16; i++) {
    const poly = document.getElementById('zpoly-' + i);
    const risk = document.getElementById('zlbl-' + i);
    const psi  = document.getElementById('zpsi-' + i);
    if (poly) {
      poly.style.fill = 'transparent';
      poly.style.stroke = 'rgba(52,211,153,0.35)';
      poly.classList.remove('risk-HIGH', 'quantum-diffusing');
    }
    if (risk) { risk.textContent = 'LOW'; risk.style.fill = 'rgba(255,255,255,0.35)'; }
    if (psi)  { psi.textContent = ''; }
    _zoneState[i] = { score: 0, risk: 'LOW', behavior: '', psi: 0 };
  }
  _zoneBehaviors = {};
  _trajHistory.length = 0;
  const tline = document.getElementById('traj-path');
  if (tline) {
    tline.setAttribute('points', '');
    tline.classList.remove('active');
  }
}

/* ── SVG Zone Definitions — Doubled B1/B2 & matching B9/B10; grid-aligned to outer/inner hex ─ */
const ZONE_DEFS = [
  { id:0,  pts:"140,272 160,100 320,220 307,297",    label:"Gate", cat:"entrance", cx:222, cy:217 },
  { id:1,  pts:"160,100 500,100 500,220 320,220",    label:"B1", cat:"north",  cx:366, cy:154 },
  { id:2,  pts:"500,100 840,100 680,220 500,220",    label:"B2", cat:"north",  cx:634, cy:154 },
  { id:3,  pts:"840,100 860,217 693,297 680,220",    label:"B3", cat:"corner", cx:774, cy:205 },
  { id:4,  pts:"860,217 880,333 707,373 693,297",    label:"B4", cat:"east",   cx:791, cy:303 },
  { id:5,  pts:"880,333 900,450 720,450 707,373",    label:"B5", cat:"east",   cx:808, cy:401 },
  /* B6–B8: inner on (720,450)→(680,760); outer on building-wall (900,450)→(840,880) — same thirds */
  { id:6,  pts:"720,450 900,450 880,593 707,553",    label:"B6", cat:"east",   cx:802, cy:512 },
  { id:7,  pts:"707,553 880,593 860,737 693,657",    label:"B7", cat:"east",   cx:785, cy:635 },
  { id:8,  pts:"693,657 860,737 840,880 680,760",    label:"B8", cat:"corner", cx:768, cy:759 },
  { id:9,  pts:"628,880 840,880 680,760 568,760",    label:"B9", cat:"south",  cx:685, cy:826 },
  /* B10 = center bottom (B9–B11); B11 = left bottom + west flank — do not merge (was overlapping) */
  { id:10, pts:"373,880 628,880 568,760 433,760",    label:"B10", cat:"south",  cx:500, cy:820 },
  { id:11, pts:"160,880 373,880 433,760 320,760 307,657 140,737", label:"B11", cat:"corner", cx:289, cy:779 },
  { id:12, pts:"140,737 120,593 293,553 307,657",    label:"B12", cat:"west",   cx:210, cy:637 },
  { id:13, pts:"120,593 100,450 280,450 293,553",    label:"B13", cat:"west",   cx:193, cy:512 },
  { id:14, pts:"100,450 120,361 293,373 280,450",    label:"B14", cat:"west",   cx:196, cy:409 },
  { id:15, pts:"120,361 140,272 307,297 293,373",    label:"B15", cat:"corner", cx:213, cy:326 },
];

// Undirected ring; keep in sync with backend/engine/pipeline.py ADJACENCY
const ZONE_ADJ = {
  0:[15, 1], 1:[0, 2], 2:[1, 3], 3:[2, 4], 4:[3, 5], 5:[4, 6], 6:[5, 7], 7:[6, 8],
  8:[7, 9], 9:[8, 10], 10:[9, 11], 11:[10, 12], 12:[11, 13], 13:[12, 14], 14:[13, 15], 15:[14, 0]
};

/* Zone index → CCTV tile id (index.html); ids are legacy labels, not zone numbers */
const CCTV_TILE_ID_BY_ZONE = { 1: 'cctv-0', 5: 'cctv-4', 0: 'cctv-15', 8: 'cctv-7' };

function resetCctvMatrixFeeds() {
  document.querySelectorAll('.cctv-feed').forEach((feed) => {
    feed.classList.remove('threat-active');
    const statusEl = feed.querySelector('.cctv-status');
    if (statusEl) statusEl.textContent = 'OK';
  });
}

function syncCctvMatrixThreat(evt, risk) {
  const pz = parseFloorZone(evt.zone_id);
  if (pz.floor !== currentFloor) return;

  resetCctvMatrixFeeds();

  if (risk !== 'HIGH' && risk !== 'CRITICAL') return;
  if (pz.localZone == null) return;

  const tileId = CCTV_TILE_ID_BY_ZONE[pz.localZone];
  const targetCam = tileId ? document.getElementById(tileId) : null;
  if (targetCam) {
    targetCam.classList.add('threat-active');
    const statusEl = targetCam.querySelector('.cctv-status');
    if (statusEl) statusEl.textContent = 'THREAT DETECTED';
  }
}

/* ── Zone runtime state ───────────────────────────────────── */
const _zoneState = {};
for (const z of ZONE_DEFS) _zoneState[z.id] = { score:0, risk:'LOW', behavior:'', psi:0 };

/* ── Build SVG zone polygons on load ──────────────────────── */
(function buildSVGMap() {
  const NS = 'http://www.w3.org/2000/svg';
  const polyG  = document.getElementById('zone-polys');
  const labelG = document.getElementById('zone-labels');
  if (!polyG || !labelG) return;

  for (const z of ZONE_DEFS) {
    // Polygon
    const poly = document.createElementNS(NS, 'polygon');
    poly.setAttribute('points', z.pts);
    poly.setAttribute('id', 'zpoly-' + z.id);
    poly.setAttribute('class', 'zone-poly');
    poly.setAttribute('data-zone', z.id);
    polyG.appendChild(poly);

    // Label text
    const txt = document.createElementNS(NS, 'text');
    txt.setAttribute('x', z.cx);
    txt.setAttribute('y', z.cy - 12);
    txt.setAttribute('class', 'zone-label-text');
    txt.textContent = z.label;
    labelG.appendChild(txt);

    // Risk text below label
    const rtxt = document.createElementNS(NS, 'text');
    rtxt.setAttribute('x', z.cx);
    rtxt.setAttribute('y', z.cy + 4);
    rtxt.setAttribute('class', 'zone-risk-text');
    rtxt.setAttribute('id', 'zlbl-' + z.id);
    rtxt.textContent = 'LOW';
    labelG.appendChild(rtxt);

    // Psi text
    const ptxt = document.createElementNS(NS, 'text');
    ptxt.setAttribute('x', z.cx);
    ptxt.setAttribute('y', z.cy + 16);
    ptxt.setAttribute('class', 'zone-psi-text');
    ptxt.setAttribute('id', 'zpsi-' + z.id);
    ptxt.textContent = '';
    labelG.appendChild(ptxt);

    // Tooltip events
    poly.addEventListener('mouseenter', (e) => showTooltip(e, z));
    poly.addEventListener('mousemove',  (e) => moveTooltip(e));
    poly.addEventListener('mouseleave', hideTooltip);
  }
})();

/* ── Score → fill color ──────────────────────────────────── */
function scoreToFill(score, risk) {
  const s = Math.min(1, Math.max(0, score));
  if (s > 0.8)      return `rgba(239,68,68,${0.35 + s * 0.5})`;
  if (s > 0.5)      return `rgba(245,158,11,${0.2 + s * 0.45})`;
  if (s > 0.2)      return `rgba(245,200,11,${0.1 + s * 0.3})`;
  return `rgba(52,211,153,${0.06 + s * 0.2})`;
}

function scoreToStroke(score, risk) {
  if (risk === 'HIGH')   return 'rgba(239,68,68,0.6)';
  if (risk === 'MEDIUM') return 'rgba(245,158,11,0.4)';
  return 'rgba(52,211,153,0.2)';
}

/* ── Update heatmap polygons ──────────────────────────────── */
function updateHeatmap(heatmap) {
  if (!Array.isArray(heatmap)) return;
  for (const h of heatmap) {
    const { floor, localZone } = parseFloorZone(h.zone_id);
    if (floor !== currentFloor || localZone == null) continue;
    const z    = localZone;
    const poly = document.getElementById('zpoly-' + z);
    const lbl  = document.getElementById('zlbl-' + z);
    if (!poly) continue;

    const risk  = h.risk || 'LOW';
    const score = Math.min(1, h.score || 0);
    _zoneState[z] = { ..._zoneState[z], score, risk };

    poly.style.fill = 'transparent';
    poly.style.stroke = scoreToStroke(score, risk);
    poly.classList.toggle('risk-HIGH', risk === 'HIGH');

    if (lbl) {
      lbl.textContent = risk;
      lbl.style.fill = (RISK_COLOR[risk] || '#666') + '99';
    }
  }
  renderPixelHeatmap();
}

/* ── Zone behavior labels ─────────────────────────────────── */
function updateZoneBehaviors(evt) {
  if (!evt) return;
  const { floor, localZone } = parseFloorZone(evt.zone_id);
  if (floor !== currentFloor || localZone == null) return;
  const zoneId = localZone;
  const risk = evt.risk_tier || 'LOW';
  const behavior = evt.behavior || '';
  const traj = evt.trajectory || {};

  if (risk !== 'LOW' && behavior) {
    let label = behavior.replace(/_/g, ' ');
    if (traj.is_suspicious) label = traj.label.toLowerCase();
    _zoneBehaviors[zoneId] = label;
    _zoneState[zoneId] = { ..._zoneState[zoneId], behavior: label };
  }
}

/* ── Quantum overlay on SVG polygons ──────────────────────── */
function updateQuantum(quantum) {
  // handled by updateQuantumOverlay for SVG
}

/* ── Inferno Colormap for Quantum Probability ──────────────── */
function getInfernoColor(t, alpha = 0.8) {
  t = Math.max(0, Math.min(1, t));
  const stops = [
    { t: 0.00, r: 0,   g: 0,   b: 4 },
    { t: 0.15, r: 40,  g: 11,  b: 84 },
    { t: 0.30, r: 101, g: 21,  b: 110 },
    { t: 0.45, r: 159, g: 42,  b: 99 },
    { t: 0.60, r: 212, g: 72,  b: 66 },
    { t: 0.75, r: 245, g: 124, b: 21 },
    { t: 0.90, r: 250, g: 193, b: 39 },
    { t: 1.00, r: 252, g: 255, b: 164 }
  ];
  let i = 1;
  while (i < stops.length && stops[i].t < t) i++;
  const s1 = stops[i - 1];
  const s2 = stops[i];
  const factor = (t - s1.t) / (s2.t - s1.t);
  const r = Math.round(s1.r + factor * (s2.r - s1.r));
  const g = Math.round(s1.g + factor * (s2.g - s1.g));
  const b = Math.round(s1.b + factor * (s2.b - s1.b));
  return `rgba(${r},${g},${b},${alpha})`;
}

/* ── Pixelated Probability Heatmap Engine ──────────────── */
function renderPixelHeatmap() {
  const canvas = document.getElementById('pixel-heatmap');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');

  // Clear the canvas for the new frame
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  // --- Hex shield clip — identical to index.html building-wall / inner-wall ---
  ctx.save();
  ctx.beginPath();

  // 1. Outer (clockwise)
  ctx.moveTo(160, 100);
  ctx.lineTo(840, 100);
  ctx.lineTo(900, 450);
  ctx.lineTo(840, 880);
  ctx.lineTo(160, 880);
  ctx.lineTo(100, 450);
  ctx.closePath();

  // 2. Inner courtyard (counter-clockwise hole)
  ctx.moveTo(320, 220);
  ctx.lineTo(280, 450);
  ctx.lineTo(320, 760);
  ctx.lineTo(680, 760);
  ctx.lineTo(720, 450);
  ctx.lineTo(680, 220);
  ctx.closePath();

  // 3. Clip using "evenodd"
  ctx.clip("evenodd");
  // -------------------------------------------------

  // Configuration
  const PIXEL_SIZE = 18;
  const SIGMA = 120;

  const cols = Math.ceil(canvas.width / PIXEL_SIZE);
  const rows = Math.ceil(canvas.height / PIXEL_SIZE);

  // Pre-fetch the current probabilities to avoid repeated lookups
  const activeZones = ZONE_DEFS.map(z => {
    return {
      cx: z.cx,
      cy: z.cy,
      psi: _zoneState[z.id]?.psi || 0,
      score: _zoneState[z.id]?.score || 0
    };
  }).filter(z => z.psi > 0.01 || z.score > 0.1);

  if (activeZones.length === 0) {
    ctx.restore(); // Make sure to restore before returning early!
    return;
  }

  // Loop through every pixel in the grid
  for (let x = 0; x < cols; x++) {
    for (let y = 0; y < rows; y++) {
      const px = x * PIXEL_SIZE + (PIXEL_SIZE / 2);
      const py = y * PIXEL_SIZE + (PIXEL_SIZE / 2);

      let pixelProbability = 0;

      for (const zone of activeZones) {
        const dx = px - zone.cx;
        const dy = py - zone.cy;
        const distSq = dx * dx + dy * dy;

        const influence = Math.exp(-distSq / (2 * SIGMA * SIGMA));
        const weight = Math.max(zone.psi * 2.0, zone.score);
        pixelProbability += influence * weight;
      }

      if (pixelProbability > 0.05) {
        const t = Math.min(1.0, pixelProbability);
        ctx.fillStyle = getInfernoColor(t, 0.75 + (t * 0.25));
        ctx.fillRect(x * PIXEL_SIZE, y * PIXEL_SIZE, PIXEL_SIZE - 1, PIXEL_SIZE - 1);
      }
    }
  }

  // --- NEW: Restore context state ---
  ctx.restore();
}

function updateQuantumOverlay(quantumField, quantumState, quantumEntropy) {
  clearAllFloorThreats();

  let localQuantumField = [];

  if (quantumField && quantumField.length > 0) {
    quantumField.forEach(q => {
      let floorStr = '1';
      let localZoneId = q.zone_id;

      if (String(q.zone_id).includes('_')) {
        const parts = String(q.zone_id).split('_');
        floorStr = parts[0].replace(/^F/i, '');
        localZoneId = zoneIndexFromZNotation(parts[1] || '');
      } else {
        localZoneId = parseInt(String(q.zone_id), 10);
        if (!Number.isFinite(localZoneId) || localZoneId < 0 || localZoneId > 15) localZoneId = NaN;
      }

      const parsedFloor = parseInt(floorStr, 10);
      const prob = Number(q.probability) || 0;

      if (parsedFloor !== currentFloor && prob > 0.15) {
        const threatBtn = document.getElementById(`btn-floor-${parsedFloor}`);
        if (threatBtn) threatBtn.classList.add('threat');
      }

      if (parsedFloor === currentFloor && Number.isFinite(localZoneId)) {
        localQuantumField.push({ zone_id: localZoneId, probability: prob });
      }
    });
  }

  quantumField = localQuantumField;

  if (quantumField.length > 0) {
    // Determine if the quantum tracker is actively pushing probabilities
    const isQuantumActive = quantumState === 'diffusing' || quantumState === 'collapsed' || quantumState === 'tracking';

    quantumField.forEach(({ zone_id, probability }) => {
      const poly = document.getElementById('zpoly-' + zone_id);
      const ptxt = document.getElementById('zpsi-' + zone_id);
      if (!poly) return;

      _zoneState[zone_id] = { ..._zoneState[zone_id], psi: probability };

      if (isQuantumActive && probability > 0.01) {
        if (ptxt) ptxt.textContent = `ψ ${probability.toFixed(2)}`;
      } else {
        if (ptxt) ptxt.textContent = '';
      }
    });
  } else {
    for (let i = 0; i < 16; i++) {
      const ptxt = document.getElementById('zpsi-' + i);
      if (ptxt) ptxt.textContent = '';
      _zoneState[i] = { ..._zoneState[i], psi: 0 };
    }
  }

  // Update the Quantum Badge UI to match the Inferno aesthetic
  const badge = document.getElementById('quantum-state-badge');
  if (badge) {
    const colors = { tracking:'#34d399', diffusing:'#f98c0a', collapsed:'#e35933', idle:'#555' };
    badge.textContent = `ψ ${quantumState || 'idle'} · H=${(quantumEntropy || 0).toFixed(2)}`;
    badge.style.color = colors[quantumState] || '#666';
    if (quantumState === 'diffusing') {
      badge.style.textShadow = "0 0 10px rgba(249, 140, 10, 0.6)";
    } else {
      badge.style.textShadow = "none";
    }
  }

  renderPixelHeatmap();
}

/* ── AQHSO placement panel (from /aqhso/placements + optimal_placements.json) ─ */
function renderAqhsoInsights(data) {
  const statusEl = document.getElementById('aqhso-placement-status');
  const listEl   = document.getElementById('aqhso-cam-list');
  const gridEl   = document.getElementById('aqhso-grid-meta');
  if (!statusEl) return;

  if (!data) {
    statusEl.innerHTML = '<span class="aqhso-warn">Could not load <code>/aqhso/placements</code> (network or server).</span>';
    if (listEl) listEl.innerHTML = '';
    if (gridEl) gridEl.textContent = '';
    return;
  }
  if (data.error) {
    statusEl.innerHTML = `<span class="aqhso-warn">${esc(data.error)} — run <code>python scripts/aqhso_grid.py</code> from the project root to generate <code>outputs/optimal_placements.json</code>.</span>`;
    if (listEl) listEl.innerHTML = '';
    if (gridEl) gridEl.textContent = '';
    return;
  }

  const ba = data.block_assignments;
  if (!Array.isArray(ba) || ba.length === 0) {
    statusEl.textContent = 'Placement file loaded but no cameras listed.';
    if (listEl) listEl.innerHTML = '';
    if (gridEl) gridEl.textContent = '';
    return;
  }

  const err = data.coverage_error_m != null ? Number(data.coverage_error_m).toFixed(3) : '—';
  const g = data.grid || {};
  const gw = g.width ?? '—';
  const gh = g.height ?? '—';
  const bm = g.block_size_m ?? '—';
  statusEl.textContent = `${ba.length} optimal cameras loaded · planning metric (coverage error) = ${err} m`;
  if (gridEl) {
    gridEl.textContent = `Planner grid: ${gw}×${gh} blocks × ${bm} m/block (same zone IDs 0–15 as this map; ▲ marks AQHSO picks).`;
  }
  if (listEl) {
    listEl.innerHTML = ba.map((b) => {
      const def = ZONE_DEFS.find(z => z.id === b.zone_id);
      const label = def ? def.label : `Zone ${b.zone_id}`;
      const cid = (b.cam_id != null ? b.cam_id : 0) + 1;
      const xm = b.x_m != null ? Number(b.x_m).toFixed(1) : '—';
      const ym = b.y_m != null ? Number(b.y_m).toFixed(1) : '—';
      return `<li><strong>Camera ${cid}</strong> → <strong>${esc(label)}</strong> <span class="muted">(zone id ${b.zone_id} · ${xm} m, ${ym} m)</span></li>`;
    }).join('');
  }
}

/* ── Camera placement ▲ markers on SVG ─────────────────────── */
function applyPlacements(data) {
  renderAqhsoInsights(data);
  const NS = 'http://www.w3.org/2000/svg';
  const camG = document.getElementById('cam-icons');
  if (camG) camG.innerHTML = '';
  if (!data || !Array.isArray(data.block_assignments)) return;
  if (!camG) return;
  _placements = {};
  for (const b of data.block_assignments) {
    _placements[b.zone_id] = true;
    const def = ZONE_DEFS.find(z => z.id === b.zone_id);
    if (!def) continue;
    const txt = document.createElementNS(NS, 'text');
    txt.setAttribute('x', def.cx + 20);
    txt.setAttribute('y', def.cy - 15);
    txt.setAttribute('class', 'cam-marker');
    txt.textContent = '▲';
    camG.appendChild(txt);
  }
}

/* ── Trajectory path drawing ──────────────────────────────── */
const _trajHistory = [];
function updateTrajectoryPath(evt) {
  if (!evt || evt.zone_id == null) return;
  const { floor, localZone } = parseFloorZone(evt.zone_id);
  if (floor !== currentFloor || localZone == null) return;
  const def = ZONE_DEFS.find(z => z.id === localZone);
  if (!def) return;
  _trajHistory.push({ x: def.cx, y: def.cy });
  if (_trajHistory.length > 10) _trajHistory.shift();

  const line = document.getElementById('traj-path');
  if (!line) return;
  const traj = evt.trajectory || {};
  if (traj.is_suspicious && _trajHistory.length >= 2) {
    line.setAttribute('points', _trajHistory.map(p => `${p.x},${p.y}`).join(' '));
    line.classList.add('active');
  } else {
    line.classList.remove('active');
  }
}

/* ── Map tooltip ──────────────────────────────────────────── */
function showTooltip(e, zoneDef) {
  const tt = document.getElementById('map-tooltip');
  const st = _zoneState[zoneDef.id] || {};
  document.getElementById('tt-zone').textContent = `Zone ${zoneDef.label} (${zoneDef.cat})`;
  document.getElementById('tt-risk').textContent = `Risk: ${st.risk || 'LOW'}`;
  document.getElementById('tt-risk').style.color = RISK_COLOR[st.risk] || '#34d399';
  document.getElementById('tt-behavior').textContent = st.behavior ? `Behavior: ${st.behavior}` : '';
  document.getElementById('tt-score').textContent = `Score: ${(st.score || 0).toFixed(3)}${st.psi > 0.01 ? ` · ψ ${st.psi.toFixed(2)}` : ''}`;
  const aqh = document.getElementById('tt-aqhso');
  if (aqh) {
    aqh.textContent = _placements[zoneDef.id]
      ? '▲ AQHSO optimal camera — this zone is in the placement plan'
      : '';
  }
  tt.style.display = 'block';
  moveTooltip(e);
}
function moveTooltip(e) {
  const tt = document.getElementById('map-tooltip');
  const container = document.getElementById('svg-map-container');
  const rect = container.getBoundingClientRect();
  tt.style.left = (e.clientX - rect.left + 12) + 'px';
  tt.style.top  = (e.clientY - rect.top - 10) + 'px';
}
function hideTooltip() {
  document.getElementById('map-tooltip').style.display = 'none';
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

/* ── Visual Ripple Effect ───────────────────────────────────── */
function spawnRipple(zoneId) {
  const def = ZONE_DEFS.find(z => z.id === zoneId);
  if (!def) return;
  const NS = 'http://www.w3.org/2000/svg';
  const circle = document.createElementNS(NS, 'circle');
  circle.setAttribute('cx', def.cx);
  circle.setAttribute('cy', def.cy);
  circle.setAttribute('class', 'ripple-circle');
  const svg = document.getElementById('hostel-svg');
  if (svg) {
    svg.appendChild(circle);
    setTimeout(() => circle.remove(), 1500);
  }
}

let _twInterval = null;

/* ── Alert card dismiss logic ──────────────────────────────── */
window.dismissAlert = function() {
  document.body.classList.remove('alert-mode');
  const alertWrap = document.getElementById('alert-card')?.parentElement;
  if (alertWrap) alertWrap.classList.remove('alert-critical');
  if (_twInterval) clearInterval(_twInterval);
};

let _lastAlertRender = 0;
let _currentAlertSignature = '';

/* ── Alert card renderer — split layout ──────────────────── */
function renderAlertCard(evt) {
  const r     = evt.reasoning || {};
  const risk  = r.risk_level || evt.risk_tier || 'LOW';
  syncCctvMatrixThreat(evt, risk);
  const pz    = parseFloorZone(evt.zone_id);

  if (pz.floor !== currentFloor || pz.localZone == null) {
    if (pz.localZone != null && (risk === 'HIGH' || risk === 'CRITICAL')) {
      const threatBtn = document.getElementById(`btn-floor-${pz.floor}`);
      if (threatBtn) threatBtn.classList.add('threat');
    }
    return;
  }
  const localZone = pz.localZone;

  // Create a unique signature for this specific ongoing event
  const sig = `${evt.zone_id}-${risk}-${evt.behavior}`;
  const now = Date.now();

  // If the same alert is firing, only allow a full DOM re-render every 2.5 seconds
  // This prevents the typewriter effect from restarting 30 times a second.
  if (sig === _currentAlertSignature && now - _lastAlertRender < 2500) {
    return;
  }
  
  _lastAlertRender = now;
  _currentAlertSignature = sig;

  const color = RISK_COLOR[risk] || RISK_COLOR.LOW;
  const ts    = evt.timestamp ? new Date(evt.timestamp).toLocaleTimeString() : '';
  const traj  = evt.trajectory || {};

  const alertWrap = document.getElementById('alert-card').parentElement;

  // Insane visual triggers
  if (risk === 'HIGH' || risk === 'CRITICAL') {
    document.body.classList.add('alert-mode');
    alertWrap.classList.add('alert-critical');
    spawnRipple(localZone);
  } else {
    document.body.classList.remove('alert-mode');
    alertWrap.classList.remove('alert-critical');
  }

  if (_twInterval) clearInterval(_twInterval);

  let summaryHtml = '';
  if (risk === 'HIGH' || risk === 'CRITICAL') {
    summaryHtml = `<div class="typewriter-text" id="tw-target"></div>`;
  } else {
    summaryHtml = `<p class="alert-summary">${esc(r.pattern_summary || evt.behavior_label || '')}</p>`;
  }

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
          <button onclick="dismissAlert()" style="margin-left:auto; padding:4px 10px; background:rgba(255,255,255,0.1); border:1px solid rgba(255,255,255,0.2); color:#fff; border-radius:4px; cursor:pointer;" onmouseover="this.style.background='rgba(255,255,255,0.2)'" onmouseout="this.style.background='rgba(255,255,255,0.1)'">Dismiss</button>
        </div>
        ${summaryHtml}
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

  if (risk === 'HIGH' || risk === 'CRITICAL') {
    const tw = document.getElementById('tw-target');
    const zoneStr = ZONE_DEFS.find(z => z.id === localZone)?.label || `Z${localZone}`;
    const bStr = evt.behavior_label || evt.behavior || '';
    const txt = `⚠ Intrusion detected...\n→ Zone ${zoneStr}\n→ Behavior: ${bStr.replace(/_/g, ' ')}\n→ Risk: ${risk}`;
    let i = 0;
    _twInterval = setInterval(() => {
      if (!tw) { clearInterval(_twInterval); return; }
      tw.textContent += txt[i];
      i++;
      if (i >= txt.length) clearInterval(_twInterval);
    }, 35);
  }
}

/* ── Event feed row ─────────────────────────────────────────── */
let _feedCount = 0;
const MAX_FEED = 100;

let _lastFeedSignature = '';
let _lastFeedTime = 0;

function addEventRow(evt) {
  const sig = `${evt.zone_id}-${evt.behavior}`;
  const now = Date.now();
  
  // Debounce identical feed events to maximum 1 per second
  if (sig === _lastFeedSignature && now - _lastFeedTime < 1000) {
      return; 
  }
  _lastFeedSignature = sig;
  _lastFeedTime = now;

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
  clearAllFloorThreats();
  // Clear SVG heatmap
  for (let i = 0; i < 16; i++) {
    const poly = document.getElementById('zpoly-' + i);
    const risk = document.getElementById('zlbl-' + i);
    const psi  = document.getElementById('zpsi-' + i);
    if (poly) {
      poly.style.fill = 'transparent';
      poly.style.stroke = 'rgba(52,211,153,0.35)';
      poly.classList.remove('risk-HIGH', 'quantum-diffusing');
    }
    if (risk) { risk.textContent = 'LOW'; risk.style.fill = 'rgba(255,255,255,0.35)'; }
    if (psi)  { psi.textContent = ''; }
    _zoneState[i] = { score:0, risk:'LOW', behavior:'', psi:0 };
  }
  document.body.classList.remove('alert-mode');
  const alertWrap = document.getElementById('alert-card')?.parentElement;
  if(alertWrap) alertWrap.classList.remove('alert-critical');
  _zoneBehaviors = {};
  
  // Clear trajectory path
  _trajHistory.length = 0;
  const tline = document.getElementById('traj-path');
  if (tline) {
    tline.setAttribute('points', '');
    tline.classList.remove('active');
  }
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
  resetCctvMatrixFeeds();
  renderPixelHeatmap();
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
        `<span style="color:#34d399">✅ Original: ${d.original?.valid ? 'VALID' : 'INVALID'}</span> ` +
        `<span style="color:#ef4444">❌ Tampered: ${d.tampered?.valid ? 'VALID' : 'REJECTED'}</span> ` +
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

/** Open packaged TV wall page; `floor` null = all floors, else 1..TOTAL_FLOORS */
function getTvDisplayUrl(floor) {
  const u = new URL('tv_display.html', window.location.href);
  if (floor != null && floor >= 1 && floor <= TOTAL_FLOORS) u.searchParams.set('floor', String(floor));
  return u.href;
}

function wireTvDisplayConnections() {
  const main = document.getElementById('tv-link-command');
  if (main) {
    main.href = getTvDisplayUrl(null);
    main.title = 'Fullscreen command display — all floors (same WebSocket feed)';
  }
  const chips = document.getElementById('tv-floor-chips');
  if (!chips) return;
  chips.innerHTML = '';
  for (let f = 1; f <= TOTAL_FLOORS; f++) {
    const a = document.createElement('a');
    a.className = 'tv-floor-chip';
    a.textContent = `F${f}`;
    a.href = getTvDisplayUrl(f);
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.title = `Wall display locked to floor ${f} only`;
    chips.appendChild(a);
  }
}

function applyInitialFloorFromQuery() {
  const p = new URLSearchParams(window.location.search);
  const f = parseInt(p.get('floor') || '', 10);
  if (Number.isFinite(f) && f >= 1 && f <= TOTAL_FLOORS) currentFloor = f;
}

function buildFloorSelector() {
  const container = document.getElementById('floor-buttons-container');
  if (!container) return;
  container.innerHTML = '';

  for (let i = TOTAL_FLOORS; i >= 1; i--) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = `floor-btn ${i === currentFloor ? 'active' : ''}`;
    btn.id = `btn-floor-${i}`;
    btn.textContent = `F${i}`;
    btn.onclick = () => {
      currentFloor = i;
      document.querySelectorAll('.floor-btn').forEach(b => {
        b.classList.remove('active');
        b.classList.remove('threat');
      });
      btn.classList.add('active');

      const title = document.querySelector('.header-title h1');
      if (title) title.textContent = `PS-003 INTRUSION MONITOR // LEVEL ${i}`;

      const canvas = document.getElementById('pixel-heatmap');
      if (canvas) canvas.getContext('2d').clearRect(0, 0, canvas.width, canvas.height);

      resetPerFloorZoneOverlays();
      resetCctvMatrixFeeds();
      if (_latestEvent) {
        const lr = _latestEvent.reasoning || {};
        const lrisk = lr.risk_level || _latestEvent.risk_tier || 'LOW';
        syncCctvMatrixThreat(_latestEvent, lrisk);
      }
      if (_latestEvent && Array.isArray(_latestEvent.heatmap)) updateHeatmap(_latestEvent.heatmap);
      if (_latestEvent && Array.isArray(_latestEvent.quantum_field)) {
        updateQuantumOverlay(
          _latestEvent.quantum_field,
          _latestEvent.quantum_state,
          _latestEvent.quantum_entropy
        );
      } else {
        updateQuantumOverlay([], 'idle', 0);
      }
    };
    container.appendChild(btn);
  }

  const title = document.querySelector('.header-title h1');
  if (title) title.textContent = `PS-003 INTRUSION MONITOR // LEVEL ${currentFloor}`;
}

function initApp() {
  applyInitialFloorFromQuery();
  buildFloorSelector();
  wireTvDisplayConnections();
  loadInitialData();
  connect();
  setInterval(pollStats, 3000);
  setInterval(measureLatency, 5000);
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
      updateTrajectoryPath(latest);
      updateFeaturePills(latest);
      updateZoneBehaviors(latest);
    })
    .catch(() => {});

  fetch(API_BASE + '/aqhso/placements')
    .then(r => r.ok ? r.json() : Promise.reject())
    .then(applyPlacements)
    .catch(() => { renderAqhsoInsights(null); });

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
    updateTrajectoryPath(evt);
    updateFeaturePills(evt);
    updateZoneBehaviors(evt);
  };
}

/* ── Boot ────────────────────────────────────────────────────── */
initApp();
