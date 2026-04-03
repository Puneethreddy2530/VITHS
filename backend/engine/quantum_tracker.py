"""
SchrodingerTracker — quantum probability field for lost intruder tracking.

When YOLO loses track of a person the intruder's location becomes uncertain.
Rather than discarding that uncertainty, we model it explicitly as a
wavefunction ψ that diffuses across adjacent zones each frame — exactly as
a quantum particle's probability density spreads under the time-dependent
Schrödinger equation.

When YOLO redetects the person in zone Z:
  ψ collapses → δ(Z)   (all probability concentrates at the measured zone)

This gives the dashboard a live heatmap showing *where the intruder probably
is* while they're out of frame — physically motivated, visually compelling.

Reference: inspired by Schrödinger (1926), "An Undulatory Theory of the
Mechanics of Atoms and Molecules", Physical Review 28(6), 1049-1070.

4×4 hostel-block grid (n_zones = 16, grid_w = 4):
  ┌───┬───┬───┬───┐
  │ 0 │ 1 │ 2 │ 3 │
  ├───┼───┼───┼───┤
  │ 4 │ 5 │ 6 │ 7 │
  ├───┼───┼───┼───┤
  │ 8 │ 9 │10 │11 │
  ├───┼───┼───┼───┤
  │12 │13 │14 │15 │
  └───┴───┴───┴───┘
"""

import numpy as np


class SchrodingerTracker:
    """
    Treats a lost intruder as a quantum probability field ψ.

    States
    ------
    tracking=True  : YOLO has active detection → ψ is a delta function.
    tracking=False : YOLO lost track          → ψ diffuses each frame.

    Public API
    ----------
    detect(zone_id) — call whenever YOLO finds a person; collapses ψ.
    lose()          — call when YOLO loses track; begins diffusion.
    diffuse(steps)  — call every frame to spread the probability field.
    field()         — returns per-zone probability list for heatmap.
    most_likely_zone() — returns argmax(ψ).
    """

    # Diffusion parameters — tuned to feel natural at 15–30 fps
    SPREAD_FACTOR = 0.25   # fraction of probability shared with each neighbour
    DECAY_FACTOR  = 0.60   # source zone retains this fraction after spreading

    def __init__(self, n_zones: int = 16, grid_w: int = 4):
        if n_zones != grid_w * grid_w:
            raise ValueError(
                f"n_zones ({n_zones}) must equal grid_w² ({grid_w}²={grid_w**2})"
            )
        self.n         = n_zones
        self.gw        = grid_w
        self.psi       = np.zeros(n_zones, dtype=np.float64)   # ψ probability field
        self.tracking  = False
        self._history  = []   # (frame_idx, zone_id or None) for audit trail

        # Precompute adjacency list for the grid (avoids rebuilding each frame)
        self._adj = self._build_adjacency()

    # ── Adjacency ──────────────────────────────────────────────────
    def _build_adjacency(self) -> dict:
        adj = {}
        for z in range(self.n):
            r, c   = divmod(z, self.gw)
            nbrs   = []
            if c > 0:              nbrs.append(z - 1)         # left
            if c < self.gw - 1:   nbrs.append(z + 1)         # right
            if r > 0:              nbrs.append(z - self.gw)   # above
            if r < self.gw - 1:   nbrs.append(z + self.gw)   # below
            adj[z] = nbrs
        return adj

    # ── Wavefunction operations ────────────────────────────────────
    def detect(self, zone_id: int):
        """
        YOLO redetected the person in 'zone_id'.
        Collapse the wavefunction: ψ → δ(zone_id).
        """
        if not (0 <= zone_id < self.n):
            raise ValueError(f"zone_id {zone_id} out of range [0, {self.n})")
        self.psi          = np.zeros(self.n, dtype=np.float64)
        self.psi[zone_id] = 1.0
        self.tracking     = True
        self._history.append({"event": "collapse", "zone_id": zone_id})

    def lose(self):
        """
        YOLO lost track — begin quantum diffusion.
        ψ retains its current shape and will spread each call to diffuse().
        """
        self.tracking = False
        self._history.append({"event": "lose"})

    def diffuse(self, steps: int = 1):
        """
        Spread probability to adjacent zones (one diffusion step per call).
        No-op if tracking=True or if ψ is essentially zero everywhere.

        The update rule per zone z:
            ψ'[nbr] += ψ[z] * SPREAD_FACTOR   for each neighbour nbr
            ψ'[z]   *= DECAY_FACTOR

        After spreading, ψ is renormalised so probabilities sum to 1.
        """
        if self.tracking or self.psi.max() < 1e-6:
            return

        for _ in range(steps):
            new_psi = self.psi.copy()
            for z in range(self.n):
                if self.psi[z] < 1e-9:
                    continue
                spread = self.psi[z] * self.SPREAD_FACTOR
                for nbr in self._adj[z]:
                    new_psi[nbr] += spread
                new_psi[z] *= self.DECAY_FACTOR   # source loses probability

            total = new_psi.sum()
            self.psi = new_psi / total if total > 1e-12 else new_psi

    # ── Query API ──────────────────────────────────────────────────
    def field(self) -> list:
        """
        Returns per-zone probability suitable for a dashboard heatmap overlay.

        Example output:
          [{"zone_id": 0, "probability": 0.0625, "risk": "LOW"}, ...]
        """
        result = []
        for i, p in enumerate(self.psi):
            pf = round(float(p), 4)
            risk = (
                "HIGH"   if pf > 0.40 else
                "MEDIUM" if pf > 0.15 else
                "LOW"
            )
            result.append({
                "zone_id":     i,
                "probability": pf,
                "risk":        risk,
            })
        return result

    def most_likely_zone(self) -> int:
        """Returns the zone with the highest probability (argmax ψ)."""
        return int(np.argmax(self.psi))

    def entropy(self) -> float:
        """
        Shannon entropy of ψ — high entropy means high location uncertainty.
        Useful as an alert signal: entropy spike → intruder disappeared.
        """
        p = self.psi[self.psi > 1e-12]   # avoid log(0)
        return float(-np.sum(p * np.log2(p)))

    def state_summary(self) -> dict:
        """
        Compact status dict for logging / API responses.
        """
        if self.tracking:
            state = "tracking"
        elif self.psi.max() >= 1e-6:
            state = "diffusing"
        else:
            state = "idle"
        entropy_val = round(self.entropy(), 4)
        return {
            "tracking":          self.tracking,
            "state":             state,
            "most_likely_zone":  self.most_likely_zone(),
            "max_probability":   round(float(self.psi.max()), 4),
            "entropy":           entropy_val,
            "entropy_bits":      entropy_val,
            "field":             self.field(),
        }

    def reset(self):
        """Clear all state — use when a new session / camera feed starts."""
        self.psi      = np.zeros(self.n, dtype=np.float64)
        self.tracking = False
        self._history.clear()
