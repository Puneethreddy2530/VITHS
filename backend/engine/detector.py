"""
Phase 1 — detector.py
Core detection engine. Three models, zero training.
  - YOLOv8-nano   : object detection (animal / person)
  - CLIP          : zero-shot semantic anomaly scoring
  - Isolation Forest : per-frame statistical anomaly
All pretrained from HuggingFace / ultralytics. No dataset needed.
"""

import cv2, time, math
import numpy as np
from PIL import Image
from datetime import datetime

import torch
from transformers import CLIPProcessor, CLIPModel
from ultralytics import YOLO
from sklearn.ensemble import IsolationForest

# ── Config ─────────────────────────────────────────────────────────
DEVICE          = "cuda" if torch.cuda.is_available() else "cpu"
YOLO_EVERY      = 3     # run YOLO every N frames
CLIP_EVERY      = 3     # run CLIP every N frames
IFOREST_EVERY   = 5     # run IForest every N frames
WARMUP_FRAMES   = 150   # collect normal features before fitting IForest
ANOMALY_THRESH  = 0.42  # CLIP anomaly probability threshold (tune this)
IFOREST_THRESH  = -0.08 # IForest score threshold (more negative = more anomalous)
CONFIDENCE_MIN  = 0.40  # minimum YOLO confidence

# YOLO class IDs we care about — NO face/person identity, just presence
WATCH_CLASSES = {
    15: "cat",
    16: "dog",
    17: "horse",
    18: "sheep",
    19: "cow",
    0:  "person",    # just presence, no ID
}

# CLIP zero-shot prompts — these replace LSTM training entirely
NORMAL_PROMPTS = [
    "a person walking calmly in a corridor",
    "empty hallway at night",
    "students walking normally",
    "quiet indoor area",
]
ANOMALY_PROMPTS = [
    "a person running fast indoors",
    "suspicious loitering near entrance",
    "animal intruding in a building",
    "unusual late night activity",
    "someone falling or fighting",
    "a dog or cat entering a building",
    "person behaving suspiciously",
]

ALL_PROMPTS = NORMAL_PROMPTS + ANOMALY_PROMPTS


class Detector:
    def __init__(self):
        print("Loading models...")
        print(f"  Device: {DEVICE}")

        # YOLOv8-nano
        print("  Loading YOLOv8-nano...")
        self.yolo = YOLO("yolov8n.pt")

        # CLIP
        print("  Loading CLIP (openai/clip-vit-base-patch32)...")
        self.clip_proc  = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
        self.clip_model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(DEVICE)
        self.clip_model.eval()

        # Pre-encode text prompts once — reuse every frame
        text_inputs = self.clip_proc(
            text=ALL_PROMPTS, return_tensors="pt", padding=True
        ).to(DEVICE)
        with torch.no_grad():
            tf = self.clip_model.get_text_features(**text_inputs)
            if hasattr(tf, "pooler_output"):
                tf = tf.pooler_output
            elif isinstance(tf, tuple):
                tf = tf[0]
            self.text_features = tf / tf.norm(dim=-1, keepdim=True)

        # Isolation Forest — fitted after warmup
        self.iforest         = IsolationForest(contamination=0.05, random_state=42)
        self.iforest_fitted  = False
        self.warmup_features = []

        self.frame_count = 0
        self.prev_gray   = None

        print("  All models loaded.\n")

    # ── Optical flow features (5-dim) ─────────────────────────────
    def _flow_features(self, gray):
        if self.prev_gray is None:
            self.prev_gray = gray
            return np.zeros(5, dtype=np.float32)
        flow = cv2.calcOpticalFlowFarneback(
            self.prev_gray, gray, None,
            pyr_scale=0.5, levels=3, winsize=15,
            iterations=3, poly_n=5, poly_sigma=1.2, flags=0
        )
        mag, ang = cv2.cartToPolar(flow[..., 0], flow[..., 1])
        self.prev_gray = gray
        return np.array([
            mag.mean(), mag.std(), mag.max(),
            ang.mean(), ang.std()
        ], dtype=np.float32)

    # ── CLIP anomaly score ─────────────────────────────────────────
    def _clip_score(self, frame_bgr):
        img = Image.fromarray(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB))
        inputs = self.clip_proc(images=img, return_tensors="pt").to(DEVICE)
        with torch.no_grad():
            img_feat = self.clip_model.get_image_features(**inputs)
            if hasattr(img_feat, "pooler_output"):
                img_feat = img_feat.pooler_output
            elif isinstance(img_feat, tuple):
                img_feat = img_feat[0]
            img_feat = img_feat / img_feat.norm(dim=-1, keepdim=True)
            sims     = (img_feat @ self.text_features.T).squeeze(0)
            probs    = sims.softmax(dim=0).cpu().numpy()

        n_normal  = len(NORMAL_PROMPTS)
        normal_p  = float(probs[:n_normal].sum())
        anomaly_p = float(probs[n_normal:].sum())

        # Which anomaly prompt fired strongest?
        top_anomaly_idx  = int(np.argmax(probs[n_normal:])) + n_normal
        top_anomaly_label = ALL_PROMPTS[top_anomaly_idx]

        return anomaly_p, top_anomaly_label

    # ── Isolation Forest score ─────────────────────────────────────
    def _iforest_score(self, features):
        if not self.iforest_fitted:
            self.warmup_features.append(features)
            if len(self.warmup_features) >= WARMUP_FRAMES:
                X = np.array(self.warmup_features)
                self.iforest.fit(X)
                self.iforest_fitted = True
                print(f"  IsolationForest fitted on {len(self.warmup_features)} frames")
            return 0.0
        return float(self.iforest.decision_function([features])[0])

    # ── YOLO detections ───────────────────────────────────────────
    def _yolo_detections(self, frame_bgr):
        results  = self.yolo(frame_bgr, verbose=False)[0]
        detections = []
        for box in results.boxes:
            cls_id = int(box.cls[0])
            conf   = float(box.conf[0])
            if cls_id in WATCH_CLASSES and conf >= CONFIDENCE_MIN:
                detections.append({
                    "class":      WATCH_CLASSES[cls_id],
                    "confidence": round(conf, 3),
                    "bbox":       [round(float(v), 1) for v in box.xyxy[0]]
                })
        return detections

    # ── Main process frame ─────────────────────────────────────────
    def process(self, frame_bgr, zone_id: int = 0):
        """
        Call this every frame.
        Returns a detection result dict.
        """
        self.frame_count += 1
        fc = self.frame_count

        gray     = cv2.cvtColor(
            cv2.resize(frame_bgr, (320, 240)),
            cv2.COLOR_BGR2GRAY
        ).astype(np.float32)
        features = self._flow_features(gray)

        yolo_dets    = []
        clip_score   = 0.0
        clip_label   = ""
        iforest_score = 0.0

        if fc % YOLO_EVERY == 0:
            yolo_dets = self._yolo_detections(frame_bgr)

        if fc % CLIP_EVERY == 0:
            clip_score, clip_label = self._clip_score(frame_bgr)

        if fc % IFOREST_EVERY == 0:
            iforest_score = self._iforest_score(features)

        # ── Combine scores into unified anomaly decision ──────────
        is_anomaly = False
        reasons    = []

        if clip_score > ANOMALY_THRESH:
            is_anomaly = True
            reasons.append(f"CLIP: {clip_label} ({clip_score:.2f})")

        if self.iforest_fitted and iforest_score < IFOREST_THRESH:
            is_anomaly = True
            reasons.append(f"IForest score: {iforest_score:.3f}")

        if yolo_dets:
            animals = [d for d in yolo_dets if d["class"] != "person"]
            if animals:
                is_anomaly = True
                reasons.append(f"Animal detected: {animals[0]['class']}")

        return {
            "frame":          fc,
            "timestamp":      datetime.utcnow().isoformat(),
            "zone_id":        zone_id,
            "is_anomaly":     is_anomaly,
            "clip_score":     round(clip_score, 4),
            "iforest_score":  round(iforest_score, 4),
            "flow_magnitude": round(float(features[0]), 4),
            "yolo_detections": yolo_dets,
            "reasons":        reasons,
            "raw_features":   features.tolist(),
        }

    # ── Draw overlay on frame ──────────────────────────────────────
    def annotate(self, frame_bgr, result: dict) -> np.ndarray:
        frame = frame_bgr.copy()
        h, w  = frame.shape[:2]

        # YOLO boxes
        for det in result["yolo_detections"]:
            x1, y1, x2, y2 = [int(v) for v in det["bbox"]]
            color = (0, 80, 220) if det["class"] == "person" else (0, 180, 80)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, f"{det['class']} {det['confidence']:.2f}",
                        (x1, y1-8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

        # Alert banner
        if result["is_anomaly"]:
            cv2.rectangle(frame, (0, 0), (w, 36), (0, 0, 180), -1)
            label = result["reasons"][0] if result["reasons"] else "Anomaly"
            cv2.putText(frame, f"ALERT  {label}",
                        (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)
        else:
            cv2.rectangle(frame, (0, 0), (w, 36), (20, 140, 20), -1)
            cv2.putText(frame, "Normal",
                        (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

        # Stats overlay
        cv2.putText(frame,
                    f"CLIP:{result['clip_score']:.2f}  "
                    f"IFor:{result['iforest_score']:.3f}  "
                    f"Mag:{result['flow_magnitude']:.2f}",
                    (10, h-12), cv2.FONT_HERSHEY_SIMPLEX,
                    0.45, (220, 220, 220), 1)

        return frame
