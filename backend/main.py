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
       then open http://localhost:8000/docs
"""
import sys, os, asyncio, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import numpy as np
from datetime import datetime
from typing import Optional
from collections import deque

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# ── Lazy imports (models load once on startup) ─────────────────────
pipeline_instance = None
memory_instance   = None
reasoning_instance = None
latest_frame      = None

app = FastAPI(title="PS-003 AI Intrusion Monitor", version="1.0.0")
app.add_middleware(CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

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
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[WARN] Webcam not found — using simulated events")
        await simulated_loop()
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    while True:
        ret, frame = cap.read()
        if not ret:
            await asyncio.sleep(0.03)
            continue

        result = pipeline_instance.process(frame, zone_id=0)
        latest_frame = pipeline_instance.annotate(frame.copy(), result)

        if result["is_anomaly"]:
            enriched  = memory_instance.process(frame, result)
            reasoning = reasoning_instance.analyze(
                enriched,
                enriched.get("similar_events", []),
                enriched.get("recurrence", 0)
            )
            event = build_event(enriched, reasoning)
            incident_log.appendleft(event)
            await broadcast(event)

        await asyncio.sleep(0.03)   # ~30fps


async def simulated_loop():
    """Simulates anomaly events for demo when no webcam."""
    import random
    behaviors = ["loitering", "fast_movement", "animal", "erratic"]
    while True:
        await asyncio.sleep(random.uniform(4, 10))
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
            "simulated": True,
        }
        event["heatmap"][zone_id]["score"] = round(random.uniform(0.5, 1.0), 3)
        event["heatmap"][zone_id]["risk"]  = event["risk_tier"]
        incident_log.appendleft(event)
        await broadcast(event)


def build_event(enriched: dict, reasoning: dict) -> dict:
    pat = enriched.get("pattern") or {}
    return {
        "id":              f"evt_{int(time.time()*1000)}",
        "timestamp":       enriched.get("timestamp", datetime.utcnow().isoformat()),
        "zone_id":         enriched.get("zone_id", 0),
        "behavior":        enriched.get("behavior", "unknown"),
        "behavior_label":  enriched.get("behavior_label", ""),
        "risk_tier":       enriched.get("risk_tier", "LOW"),
        "clip_score":      enriched.get("clip_score", 0),
        "recurrence":      enriched.get("recurrence", 0),
        "pattern_id":      pat.get("pattern_id"),
        "pattern_label":   pat.get("label"),
        "reasoning":       reasoning,
        "heatmap":         enriched.get("heatmap", []),
        "simulated":       False,
    }


async def broadcast(event: dict):
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
    return {
        "total_incidents": len(incident_log),
        "memory_events":   memory_instance._index.ntotal if memory_instance else 0,
        "uptime_s":        int(time.time()),
    }

async def video_generator():
    global latest_frame
    while True:
        if latest_frame is None:
            await asyncio.sleep(0.1)
            continue
        
        # Fast JPEG encoding
        ret, buffer = cv2.imencode('.jpg', latest_frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        if not ret:
            await asyncio.sleep(0.1)
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


if __name__ == "__main__":
    uvicorn.run("phase5.main:app", host="0.0.0.0", port=8000, reload=False)
