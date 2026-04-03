"""
Phase 5 — main.py
FastAPI backend. All detection layers unified here.
Endpoints:
  GET  /health
  GET  /heatmap           — current zone scores
  GET  /events            — recent incident log
  GET  /aqhso/placements  — camera placement from AQHSO
  WS   /ws/alerts         — real-time WebSocket alert stream

Usage: python phase5/main.py
       then open http://localhost:8888/docs
"""
import sys, os, asyncio, json, time, threading, random, copy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np
from datetime import datetime
from typing import Optional
from collections import deque

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn

# ── Lazy imports (models load once on startup) ─────────────────────
pipeline_instance  = None
memory_instance    = None
reasoning_instance = None
latest_frame       = None

from backend.core.security import sign_event, verify_event

app = FastAPI(title="PS-003 AI Intrusion Monitor", version="1.0.0")
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_startup_time = time.time()

# ── Voice dispatch (pyttsx3) ───────────────────────────────────────
try:
    import pyttsx3 as _pyttsx3
    import pythoncom  # Required for Windows COM initialization in threads
    _TTS_AVAILABLE = True
except Exception as _e:
    print(f"[WARN] pyttsx3 or pythoncom unavailable — voice alerts disabled ({_e})")
    _TTS_AVAILABLE = False

import queue

tts_queue = queue.Queue()

def _tts_worker():
    if not _TTS_AVAILABLE:
        return
    try:
        pythoncom.CoInitialize()  # Crucial fix for Windows COM threading
        import pyttsx3
        engine = pyttsx3.init()
        engine.setProperty("rate", 160)
        while True:
            msg = tts_queue.get()
            if msg is None: break
            engine.say(msg)
            engine.runAndWait()
    except Exception as e:
        print(f"[TTS Error] Thread crashed: {e}")

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

# ── Neuromorphic Event Gate ─────────────────────────────────────────
class NeuromorphicGate:
    def __init__(self, threshold=15):
        self.threshold = threshold
        self.prev_frame_gray = None
        self.sleep_mode = False

    def is_motion_event(self, frame):
        gray = cv2.cvtColor(cv2.resize(frame, (160, 120)), cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0) # Crucial: blur removes static camera noise
        if self.prev_frame_gray is None:
            self.prev_frame_gray = gray
            return True
            
        diff = cv2.absdiff(gray, self.prev_frame_gray)
        _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
        motion_pixels = cv2.countNonZero(thresh)
        self.prev_frame_gray = gray
        
        # Wake up if more than 1.5% of the pixels moved
        self.sleep_mode = motion_pixels < (160 * 120 * 0.015) 
        return not self.sleep_mode

event_gate = NeuromorphicGate()

# ── In-memory incident log (replace with DB in prod) ──────────────
incident_log: deque = deque(maxlen=500)
connected_ws: list  = []

# ── Startup: load all models ───────────────────────────────────────
@app.on_event("startup")
async def startup():
    global pipeline_instance, memory_instance, reasoning_instance
    print("\nLoading all models...")
    from backend.engine.pipeline import Pipeline
    from backend.engine.memory   import EpisodicMemory
    from backend.engine.reasoning import ReasoningEngine

    pipeline_instance  = Pipeline()
    memory_instance    = EpisodicMemory()
    reasoning_instance = ReasoningEngine()

    # Start background camera loop
    asyncio.create_task(camera_loop())
    print("All models loaded. Backend ready.\n")


# ── Background camera processing loop ─────────────────────────────
async def camera_loop():
    global latest_frame

    # Try multiple backends; test actual frame reads (not just isOpened)
    cap = None
    backends = [(cv2.CAP_DSHOW, "DSHOW"), (cv2.CAP_MSMF, "MSMF"), (cv2.CAP_ANY, "ANY")] if sys.platform.startswith('win') else [(cv2.CAP_ANY, "ANY")]
    for backend_id, backend_name in backends:
        _cap = cv2.VideoCapture(0, backend_id)
        if _cap.isOpened():
            ret, _test = _cap.read()
            if ret:
                cap = _cap
                print(f"[CAM] Using {backend_name} backend — frames OK")
                break
            else:
                print(f"[CAM] {backend_name} opened but can't read frames, trying next...")
                _cap.release()
        else:
            _cap.release()

    if cap is None:
        print("[WARN] No working webcam backend — using simulated events")
        await simulated_loop()
        return

    # Let the camera use its native resolution to avoid driver rejection

    last_sleep_mode = None
    loop = asyncio.get_event_loop()

    def _process_frame(frame):
        """Heavy ML work — runs in a thread so async loop stays free."""
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
    fail_count = 0
    MAX_FAILS = 500  # ~5 seconds at 0.01s sleep before giving up

    while True:
        # Crucial fix: cap.read() is synchronous and can block the entire FastAPI event loop 
        # on Windows if the webcam driver pauses. Force it into a background thread.
        try:
            ret, frame = await loop.run_in_executor(None, cap.read)
        except Exception as e:
            ret, frame = False, None
            
        if not ret:
            fail_count += 1
            if fail_count > MAX_FAILS:
                print("[WARN] Camera stream died — switching to simulation")
                cap.release()
                await simulated_loop()
                return
            await asyncio.sleep(0.01)
            continue
        fail_count = 0

        # Draw the last known ML results onto the immediate real-time frame
        if latest_result:
            latest_frame = pipeline_instance.annotate(frame.copy(), latest_result)
        else:
            latest_frame = frame

        # If threadpool is busy, skip ML for this frame (prevent lag buildup)
        if processing_task is None or processing_task.done():
            # Grab results from the finished background task
            if processing_task is not None:
                try:
                    annotated, res, enriched, reasoning = processing_task.result()
                    latest_result = res
                    if res["is_anomaly"] and enriched and reasoning:
                        event = build_event(enriched, reasoning)
                        incident_log.appendleft(event)
                        # broadcast using async task to prevent blocking
                        asyncio.create_task(broadcast(event))
                except Exception as e:
                    print(f"[ML Error] {e}")

            # Sleep mode processing
            if not event_gate.is_motion_event(frame):
                if last_sleep_mode != True:
                    await broadcast_system_state(True)
                    last_sleep_mode = True
                continue
            
            if last_sleep_mode != False:
                await broadcast_system_state(False)
                last_sleep_mode = False

            # Fire and forget next heavy ML task in the background
            processing_task = loop.run_in_executor(None, _process_frame, frame.copy())

        await asyncio.sleep(0.01)


async def simulated_loop():
    """Simulates anomaly events for demo when no webcam."""
    behaviors = ["loitering", "fast_movement", "animal", "erratic"]
    while True:
        await broadcast_system_state(True)
        await asyncio.sleep(random.uniform(4, 10))
        await broadcast_system_state(False)
        zone_id  = random.randint(0, 15)
        behavior = random.choice(behaviors)
        event = {
            "id":              f"evt_{int(time.time()*1000)}",
            "timestamp":       datetime.utcnow().isoformat(),
            "zone_id":         zone_id,
            "behavior":        behavior,
            "behavior_label":  behavior.replace("_", " ").title(),
            "risk_tier":       random.choice(["LOW", "MEDIUM", "HIGH"]),
            "clip_score":      round(random.uniform(0.4, 0.9), 3),
            "recurrence":      random.randint(0, 5),
            "pattern_id":      f"P{random.randint(1,20):03d}",
            "reasoning": {
                "risk_level":         "MEDIUM",
                "pattern_summary":    f"Simulated {behavior} in zone {zone_id}",
                "why_flagged":        ["Demo mode", "Simulated event"],
                "predicted_next":     "N/A (simulation)",
                "recommended_action": "No real action needed (demo)",
            },
            "heatmap": [{"zone_id": z, "score": 0.0, "risk": "LOW"} for z in range(16)],
            "quantum_field": [
                {"zone_id": i, "probability": random.uniform(0, 0.15)
                 if i != zone_id else random.uniform(0.6, 1.0)}
                for i in range(16)
            ],
            "quantum_state":   random.choice(["tracking", "diffusing", "collapsed"]),
            "quantum_entropy": round(random.uniform(0.1, 2.5), 3),
            "divergence":      round(random.uniform(-1.0, 1.0), 4),
            "curl":            round(random.uniform(-1.0, 1.0), 4),
            "lyapunov":        round(random.uniform(-0.5, 0.8), 4),
            "simulated": True,
        }
        event["heatmap"][zone_id]["score"] = round(random.uniform(0.5, 1.0), 3)
        event["heatmap"][zone_id]["risk"]  = event["risk_tier"]

        # Trajectory data for demo
        is_traj_suspicious = random.random() < 0.3
        osc = random.randint(0, 6) if is_traj_suspicious else random.randint(0, 2)
        eff = round(random.uniform(0.15, 0.34), 3) if is_traj_suspicious else round(random.uniform(0.4, 0.9), 3)
        ent = round(random.uniform(2.1, 3.0), 3) if is_traj_suspicious else round(random.uniform(0.3, 1.8), 3)
        if is_traj_suspicious:
            traj_label = random.choice(["Mule behavior", "Zigzag pattern", "Chokepoint loiter"])
        else:
            traj_label = "Normal"
        event["trajectory"] = {
            "path_entropy":            ent,
            "displacement_efficiency": eff,
            "oscillation_count":       osc,
            "is_suspicious":           is_traj_suspicious,
            "label":                   traj_label,
        }
        event["flow_magnitude"] = round(random.uniform(0.2, 5.0), 3)

        incident_log.appendleft(event)
        await broadcast(event)


def build_event(enriched: dict, reasoning: dict) -> dict:
    pat = enriched.get("pattern") or {}
    event = {
        "id":              f"evt_{int(time.time()*1000)}",
        "timestamp":       enriched.get("timestamp", datetime.utcnow().isoformat()),
        "zone_id":         enriched.get("zone_id", 0),
        "behavior":        enriched.get("behavior", "unknown"),
        "behavior_label":  enriched.get("behavior_label", ""),
        "risk_tier":       enriched.get("risk_tier", "LOW"),
        "clip_score":      enriched.get("clip_score", 0),
        "flow_magnitude":  enriched.get("flow_magnitude", 0),
        "recurrence":      enriched.get("recurrence", 0),
        "pattern_id":      pat.get("pattern_id"),
        "pattern_label":   pat.get("label"),
        "reasoning":       reasoning,
        "heatmap":         enriched.get("heatmap", []),
        # Physics / fluid-dynamics signals
        "divergence":      enriched.get("divergence", 0.0),
        "curl":            enriched.get("curl", 0.0),
        "lyapunov":        enriched.get("lyapunov", 0.0),
        # Trajectory / mule topology
        "trajectory":      enriched.get("trajectory", {}),
        # Schrödinger quantum tracker state
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
    # Voice alert for HIGH-risk events only
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
    tampered["risk_tier"] = "LOW"   # attacker downgrades risk
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
    """Inject a simulated anomaly event into the broadcast."""
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
    # Propagate to adjacent zones
    for nbr in [zone_id-1, zone_id+1, zone_id-4, zone_id+4]:
        if 0 <= nbr < 16:
            event["heatmap"][nbr]["score"] = round(score * 0.4, 3)
            event["heatmap"][nbr]["risk"]  = "MEDIUM" if req.risk.upper() == "HIGH" else "LOW"

    incident_log.appendleft(event)
    await broadcast(event)
    return {"status": "ok", "event_id": event["id"], "zone_id": zone_id}


@app.post("/demo/reset")
async def demo_reset():
    """Clear all zone scores and incident log."""
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
        # Fast JPEG encoding
        ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        
        if not ret:
            await asyncio.sleep(0.05)
            continue
            
        frame_bytes = buffer.tobytes()
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        await asyncio.sleep(0.04) # ~25 FPS max to save CPU

@app.get("/video_feed")
async def video_feed():
    return StreamingResponse(video_generator(), media_type="multipart/x-mixed-replace; boundary=frame")

# ── WebSocket ──────────────────────────────────────────────────────
@app.websocket("/ws/alerts")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_ws.append(websocket)
    # Send last 10 events on connect
    for evt in list(incident_log)[:10]:
        await websocket.send_text(json.dumps(evt))
    try:
        while True:
            await websocket.receive_text()  # keep alive
    except WebSocketDisconnect:
        if websocket in connected_ws:
            connected_ws.remove(websocket)


from fastapi.staticfiles import StaticFiles
import os

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
