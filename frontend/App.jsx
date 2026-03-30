import { useState, useEffect, useRef } from "react";

const WS_URL   = "ws://localhost:8000/ws/alerts";
const API_URL  = "http://localhost:8000";

const RISK_COLOR = { LOW: "#1D9E75", MEDIUM: "#EF9F27", HIGH: "#E24B4A", CRITICAL: "#7F77DD" };
const GRID_W = 4;

export default function App() {
  const [events,  setEvents]  = useState([]);
  const [heatmap, setHeatmap] = useState(Array(16).fill({ score: 0, risk: "LOW" }));
  const [stats,   setStats]   = useState({ total_incidents: 0, memory_events: 0 });
  const [placements, setPlacements] = useState(null);
  const [connected, setConnected]   = useState(false);
  const wsRef = useRef(null);

  // ── WebSocket ─────────────────────────────────────────────
  useEffect(() => {
    const connect = () => {
      const ws = new WebSocket(WS_URL);
      wsRef.current = ws;

      ws.onopen  = () => setConnected(true);
      ws.onclose = () => { setConnected(false); setTimeout(connect, 2000); };

      ws.onmessage = (e) => {
        const evt = JSON.parse(e.data);
        setEvents(prev => [evt, ...prev].slice(0, 100));
        if (evt.heatmap) {
          setHeatmap(evt.heatmap);
        }
      };
    };
    connect();

    // Load initial data
    fetch(`${API_URL}/stats`).then(r=>r.json()).then(setStats).catch(()=>{});
    fetch(`${API_URL}/events`).then(r=>r.json()).then(setEvents).catch(()=>{});
    fetch(`${API_URL}/aqhso/placements`).then(r=>r.json()).then(setPlacements).catch(()=>{});

    const statsTimer = setInterval(() =>
      fetch(`${API_URL}/stats`).then(r=>r.json()).then(setStats).catch(()=>{}), 5000);

    return () => { wsRef.current?.close(); clearInterval(statsTimer); };
  }, []);

  return (
    <div style={{ minHeight:"100vh", background:"#0f0f13", color:"#e0e0e0",
                  fontFamily:"system-ui,sans-serif", padding:"20px" }}>

      {/* Header */}
      <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between",
                    marginBottom:24 }}>
        <div>
          <h1 style={{ margin:0, fontSize:20, fontWeight:600, color:"#fff" }}>
            PS-003 Intrusion Monitor
          </h1>
          <p style={{ margin:0, fontSize:12, color:"#888", marginTop:4 }}>
            AQHSO · CLIP · FAISS · ST-GCN · PQC
          </p>
        </div>
        <div style={{ display:"flex", gap:12, alignItems:"center" }}>
          <PrivacyBadge />
          <StatusPill connected={connected} />
        </div>
      </div>

      {/* Top stats row */}
      <div style={{ display:"grid", gridTemplateColumns:"repeat(4,1fr)", gap:12, marginBottom:20 }}>
        <StatCard label="Total Alerts"   value={stats.total_incidents} color="#7F77DD"/>
        <StatCard label="Memory Events"  value={stats.memory_events}   color="#1D9E75"/>
        <StatCard label="Zones Monitored" value={16}                   color="#EF9F27"/>
        <StatCard label="Papers Cited"   value={8}                     color="#D85A30"/>
      </div>

      {/* Live Camera Feed */}
      <div style={{ background:"#1a1a22", borderRadius:12, padding:16, marginBottom:16 }}>
        <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:12 }}>
          <h2 style={{ margin:0, fontSize:14, fontWeight:500, color:"#aaa" }}>
            Live Camera Feed (Annotated)
          </h2>
          <span style={{ fontSize:11, color:"#1D9E75", background:"#0e1e12", padding:"4px 10px", borderRadius:12 }}>
            ● ON AIR
          </span>
        </div>
        <div style={{ backgroundColor:"#000", borderRadius:8, overflow:"hidden", aspectRatio:"16/9" }}>
          <img 
            src={`${API_URL}/video_feed`} 
            alt="Live security stream"
            style={{ width:"100%", height:"100%", objectFit:"cover" }}
          />
        </div>
      </div>

      {/* Main grid */}
      <div style={{ display:"grid", gridTemplateColumns:"1fr 1fr", gap:16 }}>
        {/* Heatmap */}
        <div style={{ background:"#1a1a22", borderRadius:12, padding:16 }}>
          <h2 style={{ margin:"0 0 12px", fontSize:14, fontWeight:500, color:"#aaa" }}>
            Hostel Block Heatmap (ST-GCN propagation)
          </h2>
          <HeatmapGrid heatmap={heatmap} placements={placements} />
          <div style={{ display:"flex", gap:16, marginTop:10, fontSize:11, color:"#666" }}>
            {["LOW","MEDIUM","HIGH"].map(r => (
              <span key={r} style={{ display:"flex", alignItems:"center", gap:4 }}>
                <span style={{ width:10, height:10, borderRadius:2,
                               background:RISK_COLOR[r], display:"inline-block"}}/>
                {r}
              </span>
            ))}
          </div>
        </div>

        {/* Latest alert card */}
        <div style={{ background:"#1a1a22", borderRadius:12, padding:16 }}>
          <h2 style={{ margin:"0 0 12px", fontSize:14, fontWeight:500, color:"#aaa" }}>
            Latest Alert — Gemini Analysis
          </h2>
          {events[0] ? <AlertCard event={events[0]} /> :
            <p style={{ color:"#555", fontSize:13 }}>Waiting for events...</p>}
        </div>
      </div>

      {/* Event feed */}
      <div style={{ background:"#1a1a22", borderRadius:12, padding:16, marginTop:16 }}>
        <h2 style={{ margin:"0 0 12px", fontSize:14, fontWeight:500, color:"#aaa" }}>
          Incident Feed — PQC Signed
        </h2>
        <div style={{ maxHeight:280, overflowY:"auto" }}>
          {events.length === 0 && <p style={{ color:"#555", fontSize:13 }}>No events yet</p>}
          {events.map(evt => <EventRow key={evt.id} evt={evt} />)}
        </div>
      </div>
    </div>
  );
}

// ── Components ─────────────────────────────────────────────────────

function HeatmapGrid({ heatmap, placements }) {
  const cells = Array(16).fill(null).map((_, i) => {
    const h = heatmap[i] || { score: 0, risk: "LOW" };
    return h;
  });

  const camZones = new Set(
    (placements?.block_assignments || []).map(b => b.zone_id)
  );

  return (
    <div style={{ display:"grid", gridTemplateColumns:"repeat(4,1fr)", gap:4 }}>
      {cells.map((cell, i) => {
        const r = Math.floor(i / GRID_W);
        const c = i % GRID_W;
        const intensity = Math.min(1, cell.score);
        const bg = cell.risk === "HIGH"   ? `rgba(226,75,74,${0.2+intensity*0.7})`  :
                   cell.risk === "MEDIUM" ? `rgba(239,159,39,${0.2+intensity*0.6})` :
                                            `rgba(29,158,117,${0.08+intensity*0.2})`;
        return (
          <div key={i} style={{
            background: bg,
            border: `1px solid ${RISK_COLOR[cell.risk]}44`,
            borderRadius: 6,
            padding: "10px 6px",
            textAlign: "center",
            position: "relative",
            transition: "background 0.3s ease",
          }}>
            <div style={{ fontSize:10, color:"#999", fontWeight:500 }}>B{i+1}</div>
            <div style={{ fontSize:11, color:RISK_COLOR[cell.risk], fontWeight:600 }}>
              {cell.risk}
            </div>
            {camZones.has(i) && (
              <span style={{ position:"absolute", top:3, right:4,
                             fontSize:9, color:"#7F77DD" }}>▲</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

function AlertCard({ event }) {
  const r = event.reasoning || {};
  const risk = r.risk_level || event.risk_tier || "LOW";
  return (
    <div style={{ fontSize:13 }}>
      <div style={{ display:"flex", gap:8, alignItems:"center", marginBottom:10 }}>
        <span style={{ background:RISK_COLOR[risk], color:"#fff",
                       padding:"2px 10px", borderRadius:20, fontSize:11,
                       fontWeight:600 }}>{risk}</span>
        <span style={{ color:"#888" }}>Zone {event.zone_id}</span>
        <span style={{ color:"#555", fontSize:11 }}>
          {new Date(event.timestamp).toLocaleTimeString()}
        </span>
      </div>

      <p style={{ margin:"0 0 8px", color:"#ddd" }}>
        {r.pattern_summary || event.behavior_label}
      </p>

      {event.pattern_id && (
        <p style={{ margin:"0 0 8px", color:"#7F77DD", fontSize:12 }}>
          Pattern {event.pattern_id} · {event.recurrence} prior occurrences
        </p>
      )}

      {r.why_flagged?.length > 0 && (
        <div style={{ marginBottom:8 }}>
          <span style={{ color:"#888", fontSize:11 }}>Why flagged:</span>
          {r.why_flagged.map((w,i) => (
            <div key={i} style={{ color:"#bbb", fontSize:12, paddingLeft:10 }}>· {w}</div>
          ))}
        </div>
      )}

      {r.predicted_next && (
        <div style={{ background:"#12121a", borderRadius:8, padding:"8px 10px", marginBottom:8 }}>
          <span style={{ color:"#EF9F27", fontSize:11, fontWeight:600 }}>Predicted: </span>
          <span style={{ color:"#ccc", fontSize:12 }}>{r.predicted_next}</span>
        </div>
      )}

      {r.recommended_action && (
        <div style={{ background:"#0e1e12", borderRadius:8, padding:"8px 10px" }}>
          <span style={{ color:"#1D9E75", fontSize:11, fontWeight:600 }}>Action: </span>
          <span style={{ color:"#ccc", fontSize:12 }}>{r.recommended_action}</span>
        </div>
      )}
    </div>
  );
}

function EventRow({ evt }) {
  const risk = evt.risk_tier || "LOW";
  return (
    <div style={{ display:"flex", alignItems:"center", gap:10, padding:"7px 0",
                  borderBottom:"1px solid #222", fontSize:12 }}>
      <span style={{ width:6, height:6, borderRadius:3, flexShrink:0,
                     background:RISK_COLOR[risk] }}/>
      <span style={{ color:"#666", minWidth:70 }}>
        {new Date(evt.timestamp).toLocaleTimeString()}
      </span>
      <span style={{ color:"#999", minWidth:50 }}>Z{evt.zone_id}</span>
      <span style={{ color:"#ccc", flex:1 }}>{evt.behavior_label}</span>
      {evt.pattern_id && (
        <span style={{ color:"#7F77DD", fontSize:11 }}>{evt.pattern_id}</span>
      )}
      <span style={{ color:RISK_COLOR[risk], fontSize:11, fontWeight:600 }}>{risk}</span>
      <span style={{ color:"#1D9E75", fontSize:10 }}>🔐</span>
    </div>
  );
}

function StatCard({ label, value, color }) {
  return (
    <div style={{ background:"#1a1a22", borderRadius:10, padding:"14px 16px" }}>
      <div style={{ fontSize:22, fontWeight:700, color }}>{value}</div>
      <div style={{ fontSize:12, color:"#666", marginTop:2 }}>{label}</div>
    </div>
  );
}

function StatusPill({ connected }) {
  return (
    <div style={{ display:"flex", alignItems:"center", gap:6, fontSize:12,
                  background:"#1a1a22", borderRadius:20, padding:"4px 12px" }}>
      <span style={{ width:7, height:7, borderRadius:"50%", flexShrink:0,
                     background: connected ? "#1D9E75" : "#E24B4A" }}/>
      {connected ? "Live" : "Connecting..."}
    </div>
  );
}

function PrivacyBadge() {
  return (
    <div style={{ fontSize:11, background:"#0e1e12", borderRadius:20,
                  padding:"4px 12px", color:"#1D9E75", border:"1px solid #1D9E7544" }}>
      🔐 Privacy 100% · No faces · PQC Signed
    </div>
  );
}
