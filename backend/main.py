"""
Phase 5 — main.py
FastAPI backend. All detection layers unified here.
"""
import sys, os, asyncio, json, time, threading, random, copy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np
from datetime import datetime
from collections import deque

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
from contextlib import asynccontextmanager
from fastapi.staticfiles import StaticFiles

from backend.core.security import sign_event, verify_event

_startup_time = time.time()

# ── Lazy imports (models load once on startup) ─────────────────────
pipeline_instance  = None
memory_instance    = None
reasoning_instance = None
latest_frame       = None
raw_frame          = None
camera_healthy     = True

# ── Voice dispatch (pyttsx3) ───────────────────────────────────────
import queue
tts_queue = queue.Queue()

try:
    import pyttsx3 as _pyttsx3
    _TTS_AVAILABLE = True
except Exception as _e:
    print(f"[WARN] pyttsx3 unavailable — voice alerts disabled ({_e})")
    _TTS_AVAILABLE = False

def _tts_worker():
    if not _TTS_AVAILABLE: return
    try:
        # Crucial Windows COM initialization to prevent the crash you saw
        if sys.platform.startswith('win'):
            import pythoncom
            pythoncom.CoInitialize()
    except Exception:
        pass
        
    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 160)
        while True:
            msg = tts_queue.get()
            if msg is None: break
            engine.say(msg)
            engine.runAndWait()
    except Exception as e:
        print(f"[TTS Error] {e}")

if _TTS_AVAILABLE:
    threading.Thread(target=_tts_worker, daemon=True).start()

_tts_last_spoken = {}
_TTS_COOLDOWN = 60.0

def speak_alert(zone_id: int, risk: str, behavior: str):
    now = time.time()
    if now - _tts_last_spoken.get(zone_id, 0) < _TTS_COOLDOWN:
        return
    _tts_last_spoken[zone_id] = now
    msg = f"Alert. Zone {zone_id}. {behavior.replace('_', ' ')}. Risk level {risk}."
    if _TTS_AVAILABLE:
        tts_queue.put(msg)

# ── Camera device name enumeration (Windows only) ─────────────────
def _get_camera_priority_indices():
    """
    Uses PowerShell to list camera friendly names in Windows device order.
    Returns indices sorted so physical (non-virtual) cameras come first.
    """
    import subprocess
    VIRTUAL_KEYWORDS = ['nothing', 'phone', 'virtual', 'link', 'obs',
                        'droid', 'iphone', 'android', 'snap', 'xsplit',
                        'manycam', 'ndi', 'droidcam', 'iriun', 'epoccam']
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-PnpDevice -Class Camera | "
             "Where-Object {$_.Status -eq 'OK'} | "
             "Select-Object -ExpandProperty FriendlyName"],
            capture_output=True, text=True, timeout=8
        )
        names = [n.strip() for n in r.stdout.strip().splitlines() if n.strip()]
        print(f"[CAMERA] Windows camera devices: {names}")

        physical, virtual = [], []
        for idx, name in enumerate(names):
            low = name.lower()
            if any(kw in low for kw in VIRTUAL_KEYWORDS):
                virtual.append((idx, name))
            else:
                physical.append((idx, name))

        # Physical cameras first, then virtual as last-resort fallback
        ordered = physical + virtual
        # Also append extra indices in case PowerShell missed one
        seen = {i for i, _ in ordered}
        for i in range(max(len(names) + 1, 4)):
            if i not in seen:
                ordered.append((i, f"Unknown@{i}"))
        return ordered
    except Exception as e:
        print(f"[CAMERA] PowerShell enum failed ({e}), trying indices 0-3")
        return [(i, f"Camera@{i}") for i in range(4)]


# ── Dedicated Camera Capture Thread ──────────────────────────────
def capture_thread_fn():
    global raw_frame, camera_healthy
    cap = None
    print("\n[CAMERA] Enumerating devices to skip virtual/phone cameras...")

    priority = _get_camera_priority_indices()

    for cam_idx, cam_name in priority:
        print(f"  Trying index {cam_idx} — '{cam_name}' (DirectShow, native res)...")
        temp_cap = cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)

        if not temp_cap.isOpened():
            print(f"    Could not open.")
            temp_cap.release()
            continue

        success = False
        for _ in range(15):
            ret, fr = temp_cap.read()
            if ret and fr is not None and np.sum(fr) > 0:
                success = True
                break
            time.sleep(0.05)

        if success:
            cap = temp_cap
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            print(f"\n[CAMERA] SUCCESS — '{cam_name}' at index {cam_idx} ({w}x{h})\n")
            break
        else:
            print(f"    Opened but stream is dead/black — skipping.")
        temp_cap.release()

    if cap is None:
        print("\n[WARN] ALL CAMERAS FAILED. Falling back to simulation.")
        camera_healthy = False
        return

    consecutive_failures = 0
    while True:
        ret, frame = cap.read()
        if ret and frame is not None:
            raw_frame = frame
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures > 30:
                print("[WARN] Camera feed died during operation.")
                camera_healthy = False
                break
        time.sleep(0.03)  # ~30fps

# ── Neuromorphic Event Gate ─────────────────────────────────────────
class NeuromorphicGate:
    def __init__(self, w=160, h=120):
        self.w = w
        self.h = h
        self.prev_frame_gray = None
        self.sleep_mode = False

    def is_motion_event(self, frame):
        gray = cv2.cvtColor(cv2.resize(frame, (self.w, self.h)), cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        if self.prev_frame_gray is None:
            self.prev_frame_gray = gray
            return True

        diff = cv2.absdiff(gray, self.prev_frame_gray)
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        motion_pixels = cv2.countNonZero(thresh)
        self.prev_frame_gray = gray

        # Require 4.5% of the frame to change to wake the pipeline (reduces ISO/brightness noise)
        threshold_pixels = int(self.w * self.h * 0.045)
        awake = motion_pixels > threshold_pixels
        self.sleep_mode = not awake
        return awake

event_gate = NeuromorphicGate()

# ── In-memory incident log ──────────────────────────────
incident_log: deque = deque(maxlen=500)
connected_ws: list  = []

# ── Startup Context (Fixes Deprecation Warning) ────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global pipeline_instance, memory_instance, reasoning_instance
    print("\nLoading all models...")
    from backend.engine.pipeline import Pipeline
    from backend.engine.memory   import EpisodicMemory
    from backend.engine.reasoning import ReasoningEngine

    pipeline_instance  = Pipeline()
    memory_instance    = EpisodicMemory()
    reasoning_instance = ReasoningEngine()

    # Start the hardware camera thread
    threading.Thread(target=capture_thread_fn, daemon=True).start()
    
    # Start the async processing loop
    asyncio.create_task(camera_loop())
    print("All models loaded. Backend ready.\n")
    yield

app = FastAPI(title="PS-003 AI Intrusion Monitor", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Background camera processing loop ─────────────────────────────
async def camera_loop():
    global latest_frame, raw_frame, camera_healthy
    
    # Wait for camera thread to wake up (30s max — PowerShell enum can take 8s+)
    wait_ticks = 0
    while raw_frame is None and camera_healthy and wait_ticks < 300:
        await asyncio.sleep(0.1)
        wait_ticks += 1

    if not camera_healthy or raw_frame is None:
        print("[WARN] Switching to simulation mode.")
        await simulated_loop()
        return

    last_sleep_mode = None
    loop = asyncio.get_event_loop()

    def _process_frame(frame):
        result = pipeline_instance.process(frame, zone_id=0)
        annotated = pipeline_instance.annotate(frame.copy(), result)
        enriched = None
        reasoning = None
        if result["is_anomaly"]:
            enriched = memory_instance.process(frame, result)
            reasoning = reasoning_instance.analyze(
                enriched,
                enriched.get("similar_events", []),
                enriched.get("recurrence", 0)
            )
        return annotated, result, enriched, reasoning

    processing_task = None
    latest_result = None

    last_alert_time = 0

    while camera_healthy:
        # Pull the frame cleanly from the background thread
        frame = raw_frame.copy() if raw_frame is not None else None
        if frame is None:
            await asyncio.sleep(0.03)
            continue

        if latest_result:
            latest_frame = pipeline_instance.annotate(frame.copy(), latest_result)
        else:
            latest_frame = frame

        if processing_task is None or processing_task.done():
            if processing_task is not None:
                try:
                    annotated, res, enriched, reasoning = processing_task.result()
                    latest_result = res
                    if res["is_anomaly"] and enriched and reasoning:
                        if time.time() - last_alert_time > 1.0:
                            last_alert_time = time.time()
                            event = build_event(enriched, reasoning)
                            incident_log.appendleft(event)
                            asyncio.create_task(broadcast(event))
                except Exception as e:
                    print(f"[ML Error] {e}")

            if not event_gate.is_motion_event(frame):
                if last_sleep_mode != True:
                    await broadcast_system_state(True)
                    last_sleep_mode = True
                await asyncio.sleep(0.03)
                continue
            
            if last_sleep_mode != False:
                await broadcast_system_state(False)
                last_sleep_mode = False

            processing_task = loop.run_in_executor(None, _process_frame, frame.copy())

        await asyncio.sleep(0.03)


async def simulated_loop():
    """Simulates anomaly events for demo when no webcam."""
    print("[INFO] Running in simulation mode. Use the frontend 'Demo Controls' to trigger events manually.")
    
    # Send system to sleep and keep it quiet instead of firing random events
    while True:
        await broadcast_system_state(True)
        await asyncio.sleep(3600)  # Sleep for an hour instead of 4-10 seconds


def build_event(enriched: dict, reasoning: dict) -> dict:
    pat = enriched.get("pattern") or {}
    
    behavior = enriched.get("behavior", "unknown")
    risk_tier = enriched.get("risk_tier", "LOW")
    if enriched.get("forced_risk") == "LOW":
        risk_tier = "LOW"
    if behavior == "normal":
        risk_tier = "LOW"
        if isinstance(reasoning, dict):
            reasoning["risk_level"] = "LOW"
            
    event = {
        "id":              f"evt_{int(time.time()*1000)}",
        "timestamp":       enriched.get("timestamp", datetime.utcnow().isoformat()),
        "zone_id":         enriched.get("zone_id", 0),
        "behavior":        behavior,
        "behavior_label":  enriched.get("behavior_label", ""),
        "risk_tier":       risk_tier,
        "clip_score":      enriched.get("clip_score", 0),
        "flow_magnitude":  enriched.get("flow_magnitude", 0),
        "recurrence":      enriched.get("recurrence", 0),
        "pattern_id":      pat.get("pattern_id"),
        "pattern_label":   pat.get("label"),
        "reasoning":       reasoning,
        "heatmap":         enriched.get("heatmap", []),
        "divergence":      enriched.get("divergence", 0.0),
        "curl":            enriched.get("curl", 0.0),
        "lyapunov":        enriched.get("lyapunov", 0.0),
        "trajectory":      enriched.get("trajectory", {}),
        "quantum":         enriched.get("quantum", {}),
        "quantum_field": [
            {"zone_id": z["zone_id"], "probability": z["probability"]}
            for z in (enriched.get("quantum", {}).get("field") or
                      [{"zone_id": i, "probability": 0.0} for i in range(16)])
        ],
        "quantum_state":   enriched.get("quantum", {}).get("state", "idle"),
        "quantum_entropy": enriched.get("quantum", {}).get("entropy", 0.0),
        "simulated":       False,
    }
    return sign_event(event)


async def broadcast_system_state(sleep_mode: bool):
    msg = {"type": "system_state", "sleep_mode": sleep_mode}
    dead = []
    for ws in connected_ws:
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_ws.remove(ws)


async def broadcast(event: dict):
    if event.get("risk_tier") == "HIGH":
        speak_alert(
            zone_id  = event.get("zone_id", 0),
            risk     = event.get("risk_tier", "HIGH"),
            behavior = event.get("behavior", "anomaly"),
        )

    dead = []
    for ws in connected_ws:
        try:
            await ws.send_text(json.dumps(event))
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_ws.remove(ws)


# ── REST Endpoints ─────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.utcnow().isoformat()}

@app.get("/heatmap")
def get_heatmap():
    if pipeline_instance:
        return pipeline_instance.propagator.heatmap()
    return [{"zone_id": z, "score": 0.0, "risk": "LOW"} for z in range(16)]


def _heat_risk_to_zone_status(risk: str) -> str:
    r = (risk or "LOW").upper()
    if r in ("HIGH", "CRITICAL"):
        return "ALERT"
    if r in ("MEDIUM", "MODERATE"):
        return "SUSPICIOUS"
    return "SAFE"


def _mobile_zones_payload():
    """102 zones for the Android app (17×6 grid); first 16 mirror backend heatmap."""
    hm = get_heatmap()
    by_zid = {int(h.get("zone_id", i)): h for i, h in enumerate(hm)}
    out = []
    for android_id in range(1, 103):
        if android_id <= 16:
            h = by_zid.get(android_id - 1, {})
            score = float(h.get("score", 0.0) or 0.0)
            risk = h.get("risk", "LOW")
            out.append({
                "id": android_id,
                "status": _heat_risk_to_zone_status(str(risk)),
                "cause": str(h.get("cause", "Monitoring")),
                "frequency": min(99, int(round(score * 20))),
                "probability": min(1.0, max(0.0, score)),
            })
        else:
            out.append({
                "id": android_id,
                "status": "SAFE",
                "cause": "Monitoring",
                "frequency": 0,
                "probability": 0.0,
            })
    return out


def _event_to_mobile_alert(e: dict) -> dict:
    risk = str(e.get("risk_tier", "LOW")).upper()
    if risk in ("HIGH", "CRITICAL"):
        status = "ALERT"
    elif risk in ("MEDIUM", "MODERATE"):
        status = "SUSPICIOUS"
    else:
        status = "SAFE"
    z_backend = int(e.get("zone_id", 0))
    zone_number = min(102, max(1, z_backend + 1))
    label = e.get("behavior_label") or e.get("behavior") or "Anomaly"
    cause = str(e.get("behavior") or "Unknown")
    ts = e.get("timestamp")
    if isinstance(ts, str):
        ts_str = ts
    else:
        ts_str = datetime.utcnow().isoformat()
    return {
        "message": f"{label} — zone {zone_number}",
        "zoneNumber": zone_number,
        "timestamp": ts_str,
        "status": status,
        "cause": cause,
    }


@app.get("/alerts")
def get_alerts_mobile(limit: int = 30):
    """Android app (Retrofit); same incidents as /events, Gson-friendly shape."""
    return [_event_to_mobile_alert(e) for e in list(incident_log)[:limit]]


@app.get("/zones")
def get_zones_mobile():
    """Android app: full 102-zone grid aligned with IncidentRepository."""
    return _mobile_zones_payload()


@app.get("/events")
def get_events(limit: int = 50):
    return list(incident_log)[:limit]

@app.get("/aqhso/placements")
def get_placements():
    try:
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "outputs", "optimal_placements.json")
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"error": "Run scripts/aqhso_grid.py first"}

@app.get("/stats")
def get_stats():
    active = 0
    avg_clip = 0.0
    if pipeline_instance:
        for z in range(16):
            if pipeline_instance.propagator.scores.get(z, 0) > 0.01:
                active += 1
    recent = list(incident_log)[:50]
    if recent:
        clips = [e.get("clip_score", 0) for e in recent]
        avg_clip = sum(clips) / len(clips) if clips else 0.0
    patterns_found = 0
    if pipeline_instance:
        patterns_found = len(pipeline_instance.patterns.all_patterns())
    return {
        "total_incidents": len(incident_log),
        "memory_events":   memory_instance._index.ntotal if memory_instance else 0,
        "active_zones":    active,
        "avg_clip_score":  round(avg_clip, 3),
        "patterns_found":  patterns_found,
        "uptime_s":        int(time.time() - _startup_time),
    }

@app.post("/demo/tamper")
async def demo_tamper():
    event = {"id": "demo_001", "zone_id": 5,
              "behavior": "loitering", "risk_tier": "HIGH",
              "timestamp": datetime.utcnow().isoformat()}
    signed   = sign_event(event.copy())
    tampered = copy.deepcopy(signed)
    tampered["risk_tier"] = "LOW"   
    original_check = verify_event(signed)
    tampered_check = verify_event(tampered)
    return {
        "original":  original_check,
        "tampered":  tampered_check,
        "signature": signed["pqc_signature"]["sha3_hash"][:32] + "...",
    }

class SimulateRequest(BaseModel):
    risk: str = "HIGH"
    behavior: str = "fast_movement"

@app.post("/demo/simulate")
async def demo_simulate(req: SimulateRequest):
    zone_id = random.randint(0, 15)
    behaviors_map = {
        "fast_movement": "Fast movement across zone",
        "loitering":     "Loitering near entrance",
        "erratic":       "Erratic / suspicious motion",
        "animal":        "Animal intrusion",
    }
    is_traj_suspicious = random.random() < 0.5
    osc = random.randint(4, 6) if is_traj_suspicious else random.randint(0, 2)
    eff = round(random.uniform(0.15, 0.30), 3) if is_traj_suspicious else round(random.uniform(0.5, 0.9), 3)
    ent = round(random.uniform(2.2, 3.0), 3) if is_traj_suspicious else round(random.uniform(0.3, 1.5), 3)
    if is_traj_suspicious:
        traj_label = random.choice(["Mule behavior", "Zigzag pattern", "Chokepoint loiter"])
    else:
        traj_label = "Normal"

    event = {
        "id":              f"evt_{int(time.time()*1000)}",
        "timestamp":       datetime.utcnow().isoformat(),
        "zone_id":         zone_id,
        "behavior":        req.behavior,
        "behavior_label":  behaviors_map.get(req.behavior, req.behavior.replace("_", " ").title()),
        "risk_tier":       req.risk.upper(),
        "clip_score":      round(random.uniform(0.5, 0.95), 3),
        "flow_magnitude":  round(random.uniform(2.5, 6.0), 3),
        "recurrence":      random.randint(1, 8),
        "pattern_id":      f"P{random.randint(1,20):03d}",
        "reasoning": {
            "risk_level":         req.risk.upper(),
            "pattern_summary":    f"Simulated {req.behavior.replace('_',' ')} detected in zone {zone_id}",
            "why_flagged":        [f"{req.behavior.replace('_',' ').title()} detected", f"Risk level: {req.risk.upper()}", "Demo simulation"],
            "predicted_next":     "Potential escalation if behavior persists",
            "recommended_action": "Dispatch security patrol to zone",
        },
        "heatmap": [{"zone_id": z, "score": 0.0, "risk": "LOW"} for z in range(16)],
        "quantum_field": [
            {"zone_id": i, "probability": random.uniform(0, 0.1)
             if i != zone_id else random.uniform(0.7, 1.0)}
            for i in range(16)
        ],
        "quantum_state":   random.choice(["tracking", "diffusing", "collapsed"]),
        "quantum_entropy": round(random.uniform(0.5, 2.5), 3),
        "divergence":      round(random.uniform(-0.8, 0.8), 4),
        "curl":            round(random.uniform(-0.8, 0.8), 4),
        "lyapunov":        round(random.uniform(-0.3, 0.6), 4),
        "trajectory": {
            "path_entropy":            ent,
            "displacement_efficiency": eff,
            "oscillation_count":       osc,
            "is_suspicious":           is_traj_suspicious,
            "label":                   traj_label,
        },
        "simulated": True,
    }
    score = round(random.uniform(0.6, 1.0), 3)
    event["heatmap"][zone_id]["score"] = score
    event["heatmap"][zone_id]["risk"]  = req.risk.upper()
    for nbr in [zone_id-1, zone_id+1, zone_id-4, zone_id+4]:
        if 0 <= nbr < 16:
            event["heatmap"][nbr]["score"] = round(score * 0.4, 3)
            event["heatmap"][nbr]["risk"]  = "MEDIUM" if req.risk.upper() == "HIGH" else "LOW"

    incident_log.appendleft(event)
    await broadcast(event)
    return {"status": "ok", "event_id": event["id"], "zone_id": zone_id}

@app.post("/demo/reset")
async def demo_reset():
    incident_log.clear()
    if pipeline_instance:
        pipeline_instance.propagator.scores.clear()
    msg = {"type": "system_reset"}
    dead = []
    for ws in connected_ws:
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            dead.append(ws)
    for ws in dead:
        connected_ws.remove(ws)
    return {"status": "ok", "message": "All zones and incidents cleared"}

async def video_generator():
    global latest_frame
    _placeholder = np.zeros((360, 480, 3), dtype=np.uint8)
    cv2.putText(_placeholder, "Camera initializing...", (70, 175),
                cv2.FONT_HERSHEY_SIMPLEX, 0.8, (80, 80, 80), 2)
    while True:
        frame = latest_frame if latest_frame is not None else _placeholder
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        
        if not ret:
            await asyncio.sleep(0.05)
            continue
            
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        await asyncio.sleep(0.04)

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(video_generator(), media_type="multipart/x-mixed-replace; boundary=frame")

@app.websocket("/ws/alerts")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_ws.append(websocket)
    for evt in list(incident_log)[:10]:
        await websocket.send_text(json.dumps(evt))
    try:
        while True:
            await websocket.receive_text() 
    except WebSocketDisconnect:
        if websocket in connected_ws:
            connected_ws.remove(websocket)


from fastapi.staticfiles import StaticFiles

frontend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.isdir(frontend_path):
    app.mount("/", StaticFiles(directory=frontend_path, html=True), name="frontend")

if __name__ == "__main__":
    import socket
    def get_free_port(default_port=8888):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('0.0.0.0', default_port))
                return default_port
            except OSError:
                s.bind(('0.0.0.0', 0))
                return s.getsockname()[1]
                
    port = get_free_port(8888)
    print(f"\n======================================")
    print(f" Starting Dashboard on Port {port}")
    print(f" Open http://localhost:{port} in your browser")
    print(f"======================================\n")
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port, reload=False)
