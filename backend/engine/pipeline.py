"""
Phase 2 — pipeline.py
Adds on top of Phase 1 detector:
  1. Behavior classifier     — loiter / fast-move / animal / normal
  2. AQHSO adaptive threshold — Δθ spike logic, night/day aware
  3. ST-GCN zone propagation  — adjacent block alert escalation
  4. Pattern ID system        — recurring anomaly fingerprinting

Usage: imported by test_pipeline.py and phase5/main.py
"""

import json, math, time
from datetime import datetime
from collections import defaultdict, deque
from typing import Optional

# ── Hostel block adjacency (4×4 grid) ─────────────────────────────
#  0  1  2  3
#  4  5  6  7
#  8  9  10 11
# 12  13 14 15
ADJACENCY: dict[int, list[int]] = {}
GRID_W = 4
for z in range(16):
    nbrs = []
    r, c = divmod(z, GRID_W)
    if c > 0:           nbrs.append(z - 1)   # left
    if c < GRID_W-1:    nbrs.append(z + 1)   # right
    if r > 0:           nbrs.append(z - GRID_W) # above
    if r < 3:           nbrs.append(z + GRID_W) # below
    ADJACENCY[z] = nbrs


# ══════════════════════════════════════════════════════════════════
# 1. BEHAVIOR CLASSIFIER
# ══════════════════════════════════════════════════════════════════

# Tunable thresholds
SPEED_LOW    = 0.8    # mag_mean below this = slow / stationary
SPEED_HIGH   = 4.5    # mag_mean above this = fast movement
LINGER_SECS  = 8.0    # seconds in same zone = loitering
CHAOS_HIGH   = 2.5    # mag_std above this = erratic

class BehaviorClassifier:
    """
    Pure rule-based classification on top of motion features.
    No extra ML — uses speed, direction chaos, duration per zone.
    Cite: rule-based behavioral analysis common in video analytics literature.
    """
    def __init__(self):
        # per-zone: deque of (timestamp, mag_mean) for loiter detection
        self._zone_history: dict[int, deque] = defaultdict(
            lambda: deque(maxlen=300)
        )

    def classify(self, result: dict) -> str:
        """
        Returns one of:
          'animal'       — dog/cat detected by YOLO
          'loitering'    — slow motion in same zone > LINGER_SECS
          'fast_movement'— high speed motion
          'erratic'      — high directional chaos
          'normal'       — nothing notable
        """
        zone_id  = result["zone_id"]
        features = result["raw_features"]   # [mag_mean, mag_std, mag_max, ang_mean, ang_std]
        mag_mean, mag_std, mag_max, ang_mean, ang_std = features
        ts       = time.time()

        # Animal → highest priority
        for det in result.get("yolo_detections", []):
            if det["class"] in ("cat", "dog", "horse", "cow", "sheep"):
                return "animal"

        # Record motion in zone
        self._zone_history[zone_id].append((ts, mag_mean))

        # Loitering: in zone for LINGER_SECS with low speed
        history = self._zone_history[zone_id]
        if len(history) >= 5:
            old_ts, _ = history[0]
            slow_count = sum(1 for _, m in history if m < SPEED_LOW)
            if (ts - old_ts) > LINGER_SECS and slow_count > len(history) * 0.7:
                return "loitering"

        # Fast movement
        if mag_mean > SPEED_HIGH:
            return "fast_movement"

        # Erratic / chaotic
        if mag_std > CHAOS_HIGH:
            return "erratic"

        return "normal"

    def get_label(self, behavior: str) -> str:
        labels = {
            "animal":        "Animal intrusion",
            "loitering":     "Loitering near entrance",
            "fast_movement": "Fast movement across zone",
            "erratic":       "Erratic / suspicious motion",
            "normal":        "Normal activity",
        }
        return labels.get(behavior, behavior)


# ══════════════════════════════════════════════════════════════════
# 2. AQHSO ADAPTIVE THRESHOLD
# ══════════════════════════════════════════════════════════════════

class AQHSOThreshold:
    """
    Extends AQHSO's adaptive Δθ rotation gate concept to
    security alert thresholds.
    - Decays (loosens) during normal daytime operation
    - Spikes (tightens) during stagnation (missed events)
    - Tightens automatically at night (22:00 – 06:00)
    """
    def __init__(self, base_clip=0.42, base_iforest=-0.08):
        self.base_clip     = base_clip
        self.base_iforest  = base_iforest
        self._stagnation   = defaultdict(int)   # per zone
        self._last_alert   = defaultdict(float) # last alert timestamp per zone
        self.STAGNATION_TIMEOUT = 300            # 5 min without alert = stagnation

    def _night_factor(self) -> float:
        h = datetime.now().hour
        return 0.75 if (h >= 22 or h < 6) else 1.0

    def _stagnation_spike(self, zone_id: int) -> float:
        stag = self._stagnation[zone_id]
        return 1.0 + 0.15 * stag    # each stagnation period tightens by 15%

    def get_thresholds(self, zone_id: int) -> dict:
        night   = self._night_factor()
        spike   = self._stagnation_spike(zone_id)
        clip_t  = self.base_clip    * night / spike
        ifor_t  = self.base_iforest * night * spike   # more negative = tighter
        return {"clip": clip_t, "iforest": ifor_t, "night": night < 1.0}

    def record_alert(self, zone_id: int):
        self._last_alert[zone_id] = time.time()
        self._stagnation[zone_id] = 0    # reset on alert

    def tick(self, zone_id: int):
        """Call once per second to track stagnation."""
        last = self._last_alert[zone_id]
        if last > 0 and (time.time() - last) > self.STAGNATION_TIMEOUT:
            self._stagnation[zone_id] += 1
            self._last_alert[zone_id] = time.time()


# ══════════════════════════════════════════════════════════════════
# 3. ST-GCN ZONE PROPAGATION
# ══════════════════════════════════════════════════════════════════

class STGCNPropagator:
    """
    Spatial-temporal alert propagation inspired by Yu et al. (2018).
    When zone A fires an alert, adjacent zones get raised sensitivity.
    Decay factor: 0.4 (same as original ST-GCN edge weights).
    """
    DECAY = 0.4

    def __init__(self, n_zones: int = 16):
        self.scores: dict[int, float] = defaultdict(float)
        self.n_zones = n_zones

    def fire(self, zone_id: int, score: float = 1.0):
        """Record an alert in zone_id and propagate to neighbours."""
        self.scores[zone_id] = max(self.scores[zone_id], score)
        for nbr in ADJACENCY.get(zone_id, []):
            propagated = score * self.DECAY
            self.scores[nbr] = max(self.scores[nbr], propagated)

    def decay_all(self, factor: float = 0.95):
        """Call every second to let scores decay over time."""
        for z in list(self.scores.keys()):
            self.scores[z] *= factor
            if self.scores[z] < 0.01:
                del self.scores[z]

    def heatmap(self) -> list[dict]:
        """Returns list of {zone_id, score, risk} for dashboard."""
        out = []
        for z in range(self.n_zones):
            s = self.scores.get(z, 0.0)
            out.append({
                "zone_id": z,
                "score":   round(s, 4),
                "risk":    "HIGH" if s > 0.7 else "MEDIUM" if s > 0.3 else "LOW"
            })
        return out


# ══════════════════════════════════════════════════════════════════
# 4. PATTERN ID SYSTEM
# ══════════════════════════════════════════════════════════════════

class PatternTracker:
    """
    Assigns Pattern IDs to recurring anomaly types per zone.
    E.g. "Pattern A03 detected — 4th occurrence"
    No face recognition. Based purely on behavior + zone + time-of-day.
    """
    def __init__(self):
        self._patterns: dict[str, dict] = {}  # key → pattern meta
        self._counter  = 0

    def _make_key(self, zone_id: int, behavior: str) -> str:
        # Bucket time-of-day into 4 slots
        h = datetime.now().hour
        slot = "night" if h < 6 or h >= 22 else \
               "morning" if h < 12 else \
               "afternoon" if h < 18 else "evening"
        return f"{zone_id}:{behavior}:{slot}"

    def record(self, zone_id: int, behavior: str) -> dict:
        key = self._make_key(zone_id, behavior)
        if key not in self._patterns:
            self._counter += 1
            pid = f"P{self._counter:03d}"
            self._patterns[key] = {
                "pattern_id":  pid,
                "zone_id":     zone_id,
                "behavior":    behavior,
                "count":       0,
                "first_seen":  datetime.utcnow().isoformat(),
                "last_seen":   None,
            }
        pat = self._patterns[key]
        pat["count"]     += 1
        pat["last_seen"]  = datetime.utcnow().isoformat()
        return {
            "pattern_id":  pat["pattern_id"],
            "occurrence":  pat["count"],
            "label": f"Pattern {pat['pattern_id']} — {pat['count']}{'st' if pat['count']==1 else 'nd' if pat['count']==2 else 'rd' if pat['count']==3 else 'th'} occurrence",
        }

    def all_patterns(self) -> list:
        return list(self._patterns.values())


# ══════════════════════════════════════════════════════════════════
# 5. TRAJECTORY / MULE TOPOLOGY ANALYZER
# ══════════════════════════════════════════════════════════════════

class TrajectoryAnalyzer:
    """
    Tracks zone-visit sequences per detection event and computes:
      1. Path entropy            — high (>2.0) = suspicious zigzag / mule
      2. Displacement efficiency — low (<0.35) = doubling back / loitering
      3. Oscillation score       — >3 reversals in 8 steps = classic mule
    """
    def __init__(self, window: int = 10):
        self.window = window
        # per-zone deque of recently visited zone_ids
        self._trails: dict[int, deque] = defaultdict(lambda: deque(maxlen=window))

    def update(self, zone_id: int, detected_zone: int) -> dict:
        """
        Record that `detected_zone` was visited in the context of
        surveillance `zone_id`, and return trajectory metrics.
        """
        trail = self._trails[zone_id]
        trail.append(detected_zone)

        entropy    = self._path_entropy(trail)
        efficiency = self._displacement_efficiency(trail)
        osc_count  = self._oscillation_count(trail)

        is_suspicious = (
            entropy > 2.0 or
            (len(trail) >= 5 and efficiency < 0.35) or
            osc_count > 3
        )

        # Determine label
        if osc_count > 3 and efficiency < 0.35:
            label = "Mule behavior"
        elif entropy > 2.0:
            label = "Zigzag pattern"
        elif len(trail) >= 5 and efficiency < 0.35:
            label = "Chokepoint loiter"
        else:
            label = "Normal"

        return {
            "path_entropy":            round(entropy, 3),
            "displacement_efficiency": round(efficiency, 3),
            "oscillation_count":       osc_count,
            "is_suspicious":           is_suspicious,
            "label":                   label,
        }

    # ── metrics ────────────────────────────────────────────────────
    @staticmethod
    def _path_entropy(trail: deque) -> float:
        """Shannon entropy over zone visit frequencies."""
        if len(trail) < 2:
            return 0.0
        counts: dict[int, int] = defaultdict(int)
        for z in trail:
            counts[z] += 1
        n = len(trail)
        h = 0.0
        for c in counts.values():
            p = c / n
            if p > 0:
                h -= p * math.log(p)
        return h

    @staticmethod
    def _displacement_efficiency(trail: deque) -> float:
        """Ratio of unique zones visited to total steps."""
        if len(trail) == 0:
            return 1.0
        return len(set(trail)) / len(trail)

    @staticmethod
    def _oscillation_count(trail: deque) -> int:
        """Count direction reversals in last 8 zone visits."""
        recent = list(trail)[-8:]
        if len(recent) < 3:
            return 0
        reversals = 0
        for i in range(2, len(recent)):
            d_prev = recent[i-1] - recent[i-2]
            d_curr = recent[i]   - recent[i-1]
            if d_prev != 0 and d_curr != 0 and (
                (d_prev > 0 and d_curr < 0) or (d_prev < 0 and d_curr > 0)
            ):
                reversals += 1
        return reversals


# ══════════════════════════════════════════════════════════════════
# UNIFIED PIPELINE (combines Phase 1 + Phase 2)
# ══════════════════════════════════════════════════════════════════

class Pipeline:
    """
    Wraps Detector + all Phase 2 components.
    Single entry point: pipeline.process(frame, zone_id) → event dict
    """
    def __init__(self):
        from backend.engine.detector       import Detector
        from backend.engine.quantum_tracker import SchrodingerTracker
        self.detector   = Detector()
        self.behavior   = BehaviorClassifier()
        self.threshold  = AQHSOThreshold()
        self.propagator = STGCNPropagator()
        self.patterns   = PatternTracker()
        self.trajectory = TrajectoryAnalyzer(window=10)
        # Quantum tracker — one per pipeline (tracks a single intruder)
        self.q_tracker  = SchrodingerTracker(n_zones=16, grid_w=4)

    def process(self, frame_bgr, zone_id: int = 0) -> dict:
        # Phase 1 detection
        result    = self.detector.process(frame_bgr, zone_id)

        # Adaptive threshold from AQHSO logic
        thresholds = self.threshold.get_thresholds(zone_id)
        self.threshold.tick(zone_id)

        # Re-evaluate anomaly with adaptive thresholds
        is_anomaly = (
            result["clip_score"]    > thresholds["clip"] or
            (self.detector.iforest_fitted and
             result["iforest_score"] < thresholds["iforest"]) or
            any(d["class"] != "person" for d in result["yolo_detections"])
        )

        # ── Schrödinger Tracker update ───────────────────────────────
        # A "person" detection in the YOLO list collapses the wavefunction.
        # No person detected → diffuse the probability field one step.
        person_dets = [d for d in result["yolo_detections"] if d["class"] == "person"]
        if person_dets:
            self.q_tracker.detect(zone_id)   # collapse ψ → δ(zone_id)
        else:
            if self.q_tracker.tracking:
                self.q_tracker.lose()         # signal loss of track
            self.q_tracker.diffuse()          # spread ψ across neighbours

        quantum_state = self.q_tracker.state_summary()

        # Behavior classification
        behavior       = self.behavior.classify(result)
        behavior_label = self.behavior.get_label(behavior)

        # Pattern ID
        pattern = None
        if is_anomaly and behavior != "normal":
            pattern = self.patterns.record(zone_id, behavior)

        # Trajectory / mule topology analysis
        traj = self.trajectory.update(zone_id, zone_id)

        # ST-GCN propagation
        if is_anomaly:
            self.threshold.record_alert(zone_id)
            self.propagator.fire(zone_id, score=min(1.0, result["clip_score"] * 1.5))
        self.propagator.decay_all()

        result.update({
            "is_anomaly":      is_anomaly,
            "behavior":        behavior,
            "behavior_label":  behavior_label,
            "pattern":         pattern,
            "thresholds":      thresholds,
            "heatmap":         self.propagator.heatmap(),
            # Quantum tracker output — drives the wavefunction heatmap overlay
            "quantum":         quantum_state,
            # Trajectory topology (mule / zigzag detection)
            "trajectory":      traj,
        })
        return result

    def annotate(self, frame_bgr, result: dict):
        return self.detector.annotate(frame_bgr, result)
