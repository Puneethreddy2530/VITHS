"""
Phase 3 — memory.py
Episodic memory layer.
  - CLIP encodes each anomaly frame → 512-dim vector
  - FAISS stores all past anomaly embeddings
  - Neural Episodic Control (Pritzel 2017) inspired recall
  - Pattern recurrence scoring

Cite: Johnson et al. (2019) FAISS · Pritzel et al. (2017) NEC
"""

import time, json
import numpy as np
from datetime import datetime
from collections import defaultdict
from typing import Optional
from PIL import Image

import torch
from transformers import CLIPProcessor, CLIPModel
import faiss

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
EMBED_DIM       = 512     # CLIP ViT-B/32 output dimension
SIMILARITY_HIGH = 0.88    # cosine similarity → "same pattern"
RECURRENCE_HIGH = 3       # occurrences → escalate to HIGH risk

class EpisodicMemory:
    """
    Stores every anomaly event as a CLIP embedding.
    On new anomaly: retrieves top-k most similar past events.
    Assigns risk tier based on recurrence count.
    """

    def __init__(self):
        print("  Loading CLIP for memory embeddings...")
        self._proc  = CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
        self._model = CLIPModel.from_pretrained("openai/clip-vit-base-patch32").to(DEVICE)
        self._model.eval()

        # FAISS inner-product index (cosine similarity after L2 normalisation)
        self._index    = faiss.IndexFlatIP(EMBED_DIM)
        self._metadata = []   # parallel list to index rows
        print("  Memory layer ready.\n")

    # ── Embed a frame ──────────────────────────────────────────────
    def _embed_frame(self, frame_bgr: np.ndarray) -> np.ndarray:
        img    = Image.fromarray(__import__("cv2").cvtColor(frame_bgr, __import__("cv2").COLOR_BGR2RGB))
        inputs = self._proc(images=img, return_tensors="pt").to(DEVICE)
        with torch.no_grad():
            feat = self._model.get_image_features(**inputs)
            if hasattr(feat, "pooler_output"):
                feat = feat.pooler_output
            elif isinstance(feat, tuple):
                feat = feat[0]
            feat = feat / feat.norm(dim=-1, keepdim=True)
        return feat.cpu().numpy().astype(np.float32)  # (1, 512)

    # ── Store an anomaly event ─────────────────────────────────────
    def remember(self, frame_bgr: np.ndarray, event: dict) -> dict:
        """
        Embed frame, store in FAISS index with metadata.
        Returns the stored event with its embedding index.
        """
        emb = self._embed_frame(frame_bgr)
        self._index.add(emb)

        meta = {
            "idx":            self._index.ntotal - 1,
            "timestamp":      datetime.utcnow().isoformat(),
            "zone_id":        event.get("zone_id", 0),
            "behavior":       event.get("behavior", "unknown"),
            "behavior_label": event.get("behavior_label", ""),
            "clip_score":     event.get("clip_score", 0),
            "pattern_id":     event.get("pattern", {}).get("pattern_id") if event.get("pattern") else None,
        }
        self._metadata.append(meta)
        return meta

    # ── Recall similar past events ─────────────────────────────────
    def recall(self, frame_bgr: np.ndarray, k: int = 3) -> list[dict]:
        """
        Find top-k most similar past anomaly events.
        Returns list of {similarity, event} dicts.
        """
        if self._index.ntotal == 0:
            return []

        emb = self._embed_frame(frame_bgr)
        k_actual = min(k, self._index.ntotal)
        sims, idxs = self._index.search(emb, k_actual)

        results = []
        for sim, idx in zip(sims[0], idxs[0]):
            if idx < 0: continue
            results.append({
                "similarity": round(float(sim), 4),
                "event":      self._metadata[idx],
            })
        return results

    # ── Recurrence scoring ─────────────────────────────────────────
    def recurrence_score(self, frame_bgr: np.ndarray) -> int:
        """
        How many past events are highly similar to this one?
        Used for risk escalation.
        """
        if self._index.ntotal == 0:
            return 0
        emb = self._embed_frame(frame_bgr)
        k   = min(50, self._index.ntotal)
        sims, _ = self._index.search(emb, k)
        return int((sims[0] >= SIMILARITY_HIGH).sum())

    # ── Risk tier ──────────────────────────────────────────────────
    def risk_tier(self, recurrence: int, behavior: str = "unknown") -> str:
        if behavior == "normal":            return "LOW"
        if recurrence >= RECURRENCE_HIGH:   return "HIGH"
        if recurrence >= 1:                 return "MEDIUM"
        return "LOW"

    # ── Full process (remember + recall + score) ───────────────────
    def process(self, frame_bgr: np.ndarray, event: dict) -> dict:
        """
        Main entry point.
        1. Recall similar past events
        2. Compute recurrence
        3. Store current event
        Returns enriched event dict with memory context.
        """
        similar   = self.recall(frame_bgr, k=3)
        recurrence = self.recurrence_score(frame_bgr)
        risk      = self.risk_tier(recurrence, event.get("behavior", "unknown"))
        stored    = self.remember(frame_bgr, event)

        return {
            **event,
            "similar_events": similar,
            "recurrence":     recurrence,
            "risk_tier":      risk,
            "memory_idx":     stored["idx"],
            "total_stored":   self._index.ntotal,
        }

    def stats(self) -> dict:
        return {"total_events": self._index.ntotal,
                "metadata":     self._metadata[-10:]}
