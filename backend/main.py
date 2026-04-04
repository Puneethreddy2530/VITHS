"""
Phase 5 — main.py
FastAPI backend. All detection layers unified here.
"""
import sys, os, asyncio, json, time, threading, random, copy, queue
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np
from datetime import datetime
from collections import deque

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, Response, FileResponse, JSONResponse
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

# Multi-stream CCTV (MJPEG); populated in lifespan
camera_streams: dict = {}
_PIPELINE_LOCK = threading.Lock()
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_alert_queue: "queue.Queue" = queue.Queue(maxsize=8)

# ── Voice dispatch (pyttsx3) ───────────────────────────────────────
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


# ── Static video paths (project root) ───────────────────────────
def _static_video_path(filename: str) -> str:
    return os.path.join(_ROOT_DIR, "frontend", "static", filename)


def _resolve_live_camera_index():
    """First working physical webcam index, or None."""
    print("\n[CAMERA] Enumerating devices for live stream 0...")
    priority = _get_camera_priority_indices()
    for cam_idx, cam_name in priority:
        print(f"  Trying index {cam_idx} — '{cam_name}'...")
        temp_cap = (
            cv2.VideoCapture(cam_idx, cv2.CAP_DSHOW)
            if sys.platform.startswith("win")
            else cv2.VideoCapture(cam_idx)
        )
        if not temp_cap.isOpened():
            temp_cap.release()
            continue
        ok = False
        for _ in range(12):
            ret, fr = temp_cap.read()
            if ret and fr is not None and np.sum(fr) > 0:
                ok = True
                break
            time.sleep(0.05)
        temp_cap.release()
        if ok:
            print(f"[CAMERA] Live stream will use index {cam_idx} — '{cam_name}'\n")
            return cam_idx
        print(f"    Dead/black — skipping.")
    print("[WARN] No working webcam found; zone 0 will use a placeholder.\n")
    return None


class CameraStream:
    """
    One capture thread per zone: resize, optional ML every Nth frame, MJPEG source for /video_feed/{zone_id}.
    """

    def __init__(self, src, zone_id: int, pipeline):
        self.zone_id = int(zone_id)
        self.pipeline = pipeline
        self.src = src
        self.latest_frame = None
        self.latest_raw = None
        self._last_result = None
        self.running = True
        self.cap = None
        self._placeholder = False
        self._file_loop = isinstance(src, str)

        if src is None:
            self._placeholder = True
        elif isinstance(src, int):
            if sys.platform.startswith("win"):
                self.cap = cv2.VideoCapture(src, cv2.CAP_DSHOW)
            else:
                self.cap = cv2.VideoCapture(src)
            if not self.cap.isOpened():
                print(f"[STREAM] Could not open webcam index {src} — placeholder for zone {zone_id}")
                self._placeholder = True
                if self.cap:
                    self.cap.release()
                    self.cap = None
        else:
            path = src
            if not os.path.isfile(path):
                print(f"[STREAM] Missing file {path} — placeholder for zone {zone_id}")
                self._placeholder = True
            else:
                self.cap = cv2.VideoCapture(path)
                if not self.cap.isOpened():
                    print(f"[STREAM] cv2 cannot open {path} — placeholder for zone {zone_id}")
                    self._placeholder = True
                    self.cap.release()
                    self.cap = None

        threading.Thread(target=self._update_loop, daemon=True).start()

    def _black_frame(self, msg: str):
        f = np.zeros((360, 480, 3), dtype=np.uint8)
        cv2.putText(f, msg, (24, 180), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (120, 120, 130), 1)
        return f

    def _update_loop(self):
        print(f"[STREAM] Booting zone {self.zone_id} from {self.src!r}...")
        frame_skip = 2
        frame_count = 0
        last_alert_wall = 0.0

        while self.running:
            if self._placeholder:
                self.latest_raw = self._black_frame(f"NO SIGNAL Z{self.zone_id}")
                self.latest_frame = self.latest_raw.copy()
                time.sleep(0.08)
                continue

            ret, frame = self.cap.read()
            if not ret or frame is None:
                if self._file_loop and self.cap is not None:
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                print(f"[ERROR] Stream zone {self.zone_id} read failed.")
                time.sleep(0.1)
                continue

            frame = cv2.resize(frame, (480, 360))
            self.latest_raw = frame.copy()
            frame_count += 1

            if frame_count % frame_skip == 0:
                try:
                    with _PIPELINE_LOCK:
                        result = self.pipeline.process(frame, zone_id=self.zone_id)
                        self._last_result = result
                        self.latest_frame = self.pipeline.annotate(frame.copy(), result)
                    if (
                        self.zone_id == 0
                        and result.get("is_anomaly")
                        and memory_instance is not None
                    ):
                        now = time.time()
                        if now - last_alert_wall >= 1.0:
                            last_alert_wall = now
                            try:
                                _alert_queue.put_nowait((frame.copy(), result))
                            except queue.Full:
                                pass
                except Exception as e:
                    print(f"[STREAM] Z{self.zone_id} pipeline error: {e}")
                    self.latest_frame = frame.copy()
            else:
                try:
                    with _PIPELINE_LOCK:
                        if self._last_result is not None:
                            self.latest_frame = self.pipeline.annotate(
                                frame.copy(), self._last_result
                            )
                        else:
                            self.latest_frame = frame.copy()
                except Exception:
                    self.latest_frame = frame.copy()

            time.sleep(0.02)

    def stop(self):
        self.running = False
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None

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
    global camera_streams, camera_healthy
    print("\nLoading all models...")
    from backend.engine.pipeline import Pipeline
    from backend.engine.memory   import EpisodicMemory
    from backend.engine.reasoning import ReasoningEngine

    pipeline_instance  = Pipeline()
    memory_instance    = EpisodicMemory()
    reasoning_instance = ReasoningEngine()

    camera_healthy = True

    live_idx = _resolve_live_camera_index()
    camera_streams = {}
    if live_idx is not None:
        camera_streams[0] = CameraStream(live_idx, 0, pipeline_instance)
    else:
        camera_streams[0] = CameraStream(None, 0, pipeline_instance)

    camera_streams[4]  = CameraStream(_static_video_path("cctv_corridor.mp4"), 4, pipeline_instance)
    camera_streams[15] = CameraStream(_static_video_path("cctv_gate.mp4"), 15, pipeline_instance)
    camera_streams[7]  = CameraStream(_static_video_path("cctv_parking.mp4"), 7, pipeline_instance)

    asyncio.create_task(alert_consumer_loop())
    asyncio.create_task(motion_broadcast_loop())
    print("All models loaded. Backend ready.\n")
    yield

    print("Shutting down streams...")
    for s in list(camera_streams.values()):
        s.stop()
    camera_streams.clear()

app = FastAPI(title="PS-003 AI Intrusion Monitor", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


async def alert_consumer_loop():
    """Zone-0 anomaly queue → memory / reasoning / WebSocket (thread-safe bridge)."""
    while True:
        await asyncio.sleep(0.05)
        if memory_instance is None or reasoning_instance is None:
            continue
        try:
            frame, result = _alert_queue.get_nowait()
        except queue.Empty:
            continue
        try:
            loop = asyncio.get_running_loop()
            enriched = await loop.run_in_executor(
                None, lambda f=frame, r=result: memory_instance.process(f, r)
            )
            reasoning = await loop.run_in_executor(
                None,
                lambda: reasoning_instance.analyze(
                    enriched,
                    enriched.get("similar_events", []),
                    enriched.get("recurrence", 0),
                ),
            )
            event = build_event(enriched, reasoning)
            incident_log.appendleft(event)
            await broadcast(event)
        except Exception as e:
            print(f"[Alert queue] {e}")


async def motion_broadcast_loop():
    """Sleep / wake UI from live zone-0 motion (neuromorphic gate)."""
    last_sleep_mode = None
    while True:
        await asyncio.sleep(0.05)
        s0 = camera_streams.get(0)
        if s0 is None or s0.latest_raw is None:
            continue
        frame = s0.latest_raw
        awake = event_gate.is_motion_event(frame)
        if not awake:
            if last_sleep_mode is not True:
                await broadcast_system_state(True)
                last_sleep_mode = True
        else:
            if last_sleep_mode is not False:
                await broadcast_system_state(False)
                last_sleep_mode = False


async def simulated_loop():
    """Multi-floor synthetic injects when no webcam — F1 + occasional F7 breach for Z-axis UI demo."""
    print("[INFO] Simulation mode: auto-inject F1/F7; use Demo Controls for manual scenarios.")
    dummy_frame = np.zeros((120, 160, 3), dtype=np.uint8)
    await broadcast_system_state(False)

    while True:
        await asyncio.sleep(0.03)

        if not pipeline_instance or not memory_instance:
            continue

        # --- 3D multi-floor demo injection (~3% per tick @ ~30 Hz) ---
        if np.random.rand() >= 0.03:
            continue

        target_floor = 1 if np.random.rand() < 0.8 else 7
        zone = int(np.random.randint(1, 16))
        risk_str = "HIGH" if np.random.rand() < 0.3 else "MEDIUM"
        zone_id_str = f"F{target_floor}_Z{zone}"
        lz_idx = zone - 1

        if target_floor == 1:
            pipeline_instance.q_tracker.detect(lz_idx)
            qs = pipeline_instance.q_tracker.state_summary()
            quantum_field = qs["field"]
        else:
            quantum_field = [
                {
                    "zone_id": f"F{target_floor}_Z{i + 1}",
                    "probability": round(0.9 if i == lz_idx else 0.02, 4),
                    "risk": (
                        "HIGH"
                        if i == lz_idx and risk_str == "HIGH"
                        else ("MEDIUM" if i == lz_idx else "LOW")
                    ),
                }
                for i in range(16)
            ]
            qs = {
                "tracking": True,
                "state": "tracking",
                "most_likely_zone": lz_idx,
                "max_probability": 0.9,
                "entropy": 0.35,
                "field": quantum_field,
            }

        heatmap = []
        for i in range(16):
            hot = i == lz_idx
            heatmap.append({
                "zone_id": f"F{target_floor}_Z{i + 1}",
                "score": 0.88 if hot else 0.04,
                "risk": risk_str if hot else "LOW",
            })

        sim_core = {
            "zone_id": zone_id_str,
            "timestamp": datetime.utcnow().isoformat(),
            "behavior": "elevator_breach" if target_floor != 1 else "simulated_motion",
            "behavior_label": (
                "Elevator / stairwell breach (sim)"
                if target_floor != 1
                else "Simulated corridor motion"
            ),
            "risk_tier": risk_str,
            "clip_score": 0.65,
            "flow_magnitude": 4.2,
            "recurrence": int(np.random.randint(0, 4)),
            "divergence": 1.2,
            "curl": 0.35,
            "lyapunov": 0.25,
            "trajectory": {
                "path_entropy": 2.0,
                "displacement_efficiency": 0.4,
                "oscillation_count": 3,
                "is_suspicious": True,
                "label": "Simulated pattern",
            },
            "yolo_detections": [
                {"class": "person", "confidence": 0.88, "bbox": [10, 10, 50, 50]}
            ],
            "forced_risk": None,
            "heatmap": heatmap,
            "quantum": qs,
            "quantum_field": [
                {"zone_id": q["zone_id"], "probability": q["probability"]} for q in quantum_field
            ],
            "quantum_state": qs["state"],
            "quantum_entropy": qs.get("entropy", 0.0),
            "reasoning": {
                "risk_level": risk_str,
                "pattern_summary": (
                    "Simulated multi-vector anomaly detected in continuous tracking loop."
                ),
                "why_flagged": [
                    "Synthetic demo inject",
                    f"Floor {target_floor} zone activity",
                    "Person track (simulated)",
                ],
                "predicted_next": "Monitor adjacent floors for vertical movement",
                "recommended_action": "Verify stairwell / elevator cameras",
            },
            "simulated": True,
        }

        try:
            memory_instance.remember(dummy_frame, sim_core)
        except Exception as e:
            print(f"[sim] memory.remember skipped: {e}")

        event = {
            "id": f"evt_{int(time.time()*1000)}",
            **sim_core,
            "pattern_id": None,
        }
        incident_log.appendleft(event)
        await broadcast(event)


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
                      [{"zone_id": f"F1_Z{i}", "probability": 0.0} for i in range(16)])
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

def _demo_event_shell() -> dict:
    return {
        "timestamp": datetime.utcnow().isoformat(),
        "simulated": True,
        "heatmap": [{"zone_id": z, "score": 0.0, "risk": "LOW"} for z in range(16)],
        "quantum_field": [{"zone_id": i, "probability": 0.0} for i in range(16)],
        "quantum_state": "idle",
        "quantum_entropy": 0.0,
        "divergence": 0.0,
        "curl": 0.0,
        "lyapunov": 0.0,
        "trajectory": {
            "is_suspicious": False,
            "path_entropy": 0.0,
            "displacement_efficiency": 1.0,
            "oscillation_count": 0,
            "label": "Normal",
        },
        "clip_score": 0.5,
        "flow_magnitude": 1.0,
        "recurrence": 0,
        "pattern_id": None,
        "pattern_label": None,
    }


def _quantum_field_from_sparse(weights: dict[int, float]) -> list:
    """16-zone quantum_field; unspecified zones → 0."""
    return [{"zone_id": i, "probability": round(float(weights.get(i, 0.0)), 4)} for i in range(16)]


@app.post("/demo/scenario/{name}")
async def demo_scenario(name: str):
    events_to_emit = []

    if name == "faiss_memory":
        zone_id = 2
        for i in range(1, 4):
            evt = copy.deepcopy(_demo_event_shell())
            rt = "MEDIUM" if i < 3 else "HIGH"
            evt.update(
                {
                    "id": f"demo_faiss_{i}_{int(time.time() * 1000)}",
                    "zone_id": zone_id,
                    "behavior": "loitering",
                    "behavior_label": f"Loitering near entrance (Hit #{i})",
                    "risk_tier": rt,
                    "clip_score": 0.85,
                    "flow_magnitude": 1.2,
                    "recurrence": i,
                    "pattern_id": "P042" if i > 1 else None,
                }
            )
            evt["heatmap"][zone_id] = {"zone_id": zone_id, "score": 0.9, "risk": rt}
            events_to_emit.append(evt)

    elif name == "quantum_diffusion":
        zone_id = 4
        evt = copy.deepcopy(_demo_event_shell())
        qweights = {4: 0.4, 3: 0.2, 5: 0.2, 0: 0.1, 8: 0.1}
        evt.update(
            {
                "id": f"demo_quant_{int(time.time() * 1000)}",
                "zone_id": zone_id,
                "behavior": "fast_movement",
                "behavior_label": "Intruder lost - tracking probability",
                "risk_tier": "HIGH",
                "quantum_state": "diffusing",
                "quantum_entropy": 1.4,
                "quantum_field": _quantum_field_from_sparse(qweights),
            }
        )
        evt["heatmap"][zone_id] = {"zone_id": zone_id, "score": 0.7, "risk": "HIGH"}
        events_to_emit.append(evt)

    elif name == "trajectory_mule":
        zone_id = 13
        evt = copy.deepcopy(_demo_event_shell())
        evt.update(
            {
                "id": f"demo_traj_{int(time.time() * 1000)}",
                "zone_id": zone_id,
                "behavior": "erratic",
                "behavior_label": "Erratic motion",
                "risk_tier": "HIGH",
                "trajectory": {
                    "path_entropy": 2.8,
                    "displacement_efficiency": 0.25,
                    "oscillation_count": 4,
                    "is_suspicious": True,
                    "label": "Mule behavior",
                },
            }
        )
        evt["heatmap"][zone_id] = {"zone_id": zone_id, "score": 0.88, "risk": "HIGH"}
        events_to_emit.append(evt)

    elif name == "physics_chaos":
        zone_id = 10
        evt = copy.deepcopy(_demo_event_shell())
        evt.update(
            {
                "id": f"demo_phys_{int(time.time() * 1000)}",
                "zone_id": zone_id,
                "behavior": "erratic",
                "behavior_label": "Crowd Panic / Fight detected",
                "risk_tier": "CRITICAL",
                "divergence": 2.4,
                "curl": -1.8,
                "lyapunov": 0.45,
                "flow_magnitude": 6.5,
            }
        )
        evt["heatmap"][zone_id] = {"zone_id": zone_id, "score": 1.0, "risk": "CRITICAL"}
        events_to_emit.append(evt)

    else:
        return JSONResponse(
            {"detail": f"Unknown scenario '{name}'"},
            status_code=404,
        )

    title = name.replace("_", " ").title()
    for e in events_to_emit:
        e["reasoning"] = {
            "risk_level": e["risk_tier"],
            "pattern_summary": f"Demo Scenario: {title}",
            "why_flagged": [
                "Triggered by presentation demo controls",
                f"Targeted system: {name}",
            ],
            "predicted_next": "System functioning as expected",
            "recommended_action": "Explain this feature to the judges",
        }
        signed = sign_event(copy.deepcopy(e))
        incident_log.appendleft(signed)
        await broadcast(signed)
        await asyncio.sleep(1.5)

    return {"status": "ok", "scenario": name, "events": len(events_to_emit)}


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

def _mjpeg_placeholder(text: str = "Stream starting..."):
    img = np.zeros((360, 480, 3), dtype=np.uint8)
    cv2.putText(img, text, (48, 175), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (88, 88, 96), 2)
    return img


def dynamic_video_generator(zone_id: int):
    while True:
        stream = camera_streams.get(zone_id)
        if stream is None or not stream.running:
            frame = _mjpeg_placeholder("Stream offline")
        elif stream.latest_frame is not None:
            frame = stream.latest_frame
        else:
            frame = _mjpeg_placeholder()
        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
            )
        time.sleep(0.05)


_MJPEG_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}


@app.get("/video_feed/{zone_id}")
async def video_feed_zone(zone_id: int):
    if zone_id not in camera_streams:
        return Response(status_code=404)
    return StreamingResponse(
        dynamic_video_generator(zone_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers=_MJPEG_HEADERS,
    )


@app.get("/video_feed")
async def video_feed_legacy():
    """Legacy single-feed URL → zone 0 (live webcam)."""
    if 0 not in camera_streams:
        return Response(status_code=503)
    return StreamingResponse(
        dynamic_video_generator(0),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers=_MJPEG_HEADERS,
    )

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

frontend_path = os.path.join(_ROOT_DIR, "frontend")
static_path = os.path.join(frontend_path, "static")


def _frontend_file(name: str) -> str:
    return os.path.join(frontend_path, name)


# ── Dashboard static files (never mount StaticFiles at "/" — it intercepts /video_feed/* ) ──
@app.get("/")
async def serve_dashboard():
    p = _frontend_file("index.html")
    if not os.path.isfile(p):
        return Response(status_code=404, content="frontend/index.html missing")
    return FileResponse(p)


@app.get("/index.html")
async def serve_dashboard_index():
    return FileResponse(_frontend_file("index.html"))


@app.get("/style.css")
async def serve_style_css():
    return FileResponse(_frontend_file("style.css"))


@app.get("/app.js")
async def serve_app_js_file():
    return FileResponse(_frontend_file("app.js"))


@app.get("/tv_display.html")
async def serve_tv_display_html():
    return FileResponse(_frontend_file("tv_display.html"))


@app.get("/favicon.png")
async def serve_favicon_png():
    p = _frontend_file("favicon.png")
    if os.path.isfile(p):
        return FileResponse(p)
    return Response(status_code=204)


if os.path.isdir(static_path):
    app.mount("/static", StaticFiles(directory=static_path), name="cctv_static")

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
