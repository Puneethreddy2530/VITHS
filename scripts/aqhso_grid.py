"""
Phase 0 — aqhso_grid.py
Runs AQHSO on a hostel block grid (your WSN Track 3 adapted).
Outputs:
  outputs/aqhso_convergence.png    ← slide-ready plot
  outputs/optimal_placements.json  ← used by detection pipeline

Usage: python phase0/aqhso_grid.py
"""
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import json, os, time, math

os.makedirs("outputs", exist_ok=True)

# ── Hostel grid config ─────────────────────────────────────────────
GRID_W      = 4          # blocks wide
GRID_H      = 4          # blocks tall
BLOCK_M     = 15         # metres per block → 60×60m total
N_CAMERAS   = 6          # cameras to optimally place
AREA_W      = GRID_W * BLOCK_M   # 60m
AREA_H      = GRID_H * BLOCK_M   # 60m

# Entry/exit anchor points (doors, gates) — adjust for your hostel
ANCHORS = np.array([
    [0,      0     ],   # main gate
    [AREA_W, 0     ],   # east entrance
    [0,      AREA_H],   # west corridor end
    [AREA_W/2, AREA_H], # north exit
], dtype=float)

# Camera placement candidates — centre of each block
CANDIDATES = np.array([
    [(x + 0.5) * BLOCK_M, (y + 0.5) * BLOCK_M]
    for y in range(GRID_H) for x in range(GRID_W)
], dtype=float)   # 16 candidates

# ── Objective function ─────────────────────────────────────────────
def coverage_error(indices):
    selected = CANDIDATES[indices]
    # max uncovered distance across all 16 positions
    error = sum(np.linalg.norm(selected - c, axis=1).min()
                for c in CANDIDATES) / len(CANDIDATES)
    # reward cameras near entrances
    for anchor in ANCHORS:
        d = np.linalg.norm(selected - anchor, axis=1).min()
        error -= 4.0 * math.exp(-d / 15.0)
    return error

# ── OBL initialisation ─────────────────────────────────────────────
def obl_init(pop, n):
    opp = []
    for ind in pop:
        rest = list(set(range(n)) - set(ind))
        np.random.shuffle(rest)
        opp.append(rest[:len(ind)])
    return (pop + opp)[:len(pop)*2]

# ── Adaptive quantum rotation (AQHSO Δθ) ──────────────────────────
def delta_theta(base, epoch, max_ep, stagnation):
    decay = base * (1 - epoch / max_ep)
    spike = 1.0 + 0.3 * stagnation
    return decay * spike

# ── AQHSO main loop ────────────────────────────────────────────────
def aqhso(seed=42, pop_size=40, max_epochs=300):
    np.random.seed(seed)
    n = len(CANDIDATES)

    # init half, OBL doubles to pop_size
    half = [list(np.random.choice(n, N_CAMERAS, replace=False))
            for _ in range(pop_size // 2)]
    pop  = [list(p) for p in obl_init(half, n)][:pop_size]
    fit  = [coverage_error(p) for p in pop]

    best_idx  = int(np.argmin(fit))
    best_sol  = pop[best_idx][:]
    best_fit  = fit[best_idx]
    stag, prev = 0, best_fit
    conv = []

    for epoch in range(max_epochs):
        phase = epoch / max_epochs

        for i in range(pop_size):
            if phase < 0.2:
                # Phase 1 — GWO hierarchy
                new = pop[int(np.argmin(fit))][:]
                for _ in range(max(1, int(N_CAMERAS * (0.2 - phase) / 0.2))):
                    pos = np.random.randint(N_CAMERAS)
                    c   = np.random.randint(n)
                    while c in new: c = np.random.randint(n)
                    new[pos] = c

            elif phase < 0.7:
                # Phase 2 — FA quantum attraction in θ-space
                dt  = delta_theta(0.05 * np.pi, epoch, max_epochs, stag)
                att = math.exp(-0.1 * abs(1/(1+fit[i]) - 1/(1+best_fit)))
                new = best_sol[:]
                for _ in range(max(1, int(N_CAMERAS * dt / np.pi * att))):
                    pos = np.random.randint(N_CAMERAS)
                    c   = np.random.randint(n)
                    while c in new: c = np.random.randint(n)
                    new[pos] = c

            else:
                # Phase 3 — Lévy flight tunneling
                beta  = 1.5
                sigma = (math.gamma(1+beta) * math.sin(math.pi*beta/2) /
                         (math.gamma((1+beta)/2) * beta * 2**((beta-1)/2)))**(1/beta)
                levy  = np.random.randn(N_CAMERAS)*sigma / (np.abs(np.random.randn(N_CAMERAS))**(1/beta))
                new   = best_sol[:]
                for _ in range(max(1, int(min(abs(float(levy.mean())), N_CAMERAS)))):
                    pos = np.random.randint(N_CAMERAS)
                    c   = np.random.randint(n)
                    while c in new: c = np.random.randint(n)
                    new[pos] = c

            nf = coverage_error(new)
            if nf < fit[i]:
                pop[i], fit[i] = new, nf
            if nf < best_fit:
                best_sol, best_fit = new[:], nf

        stag = stag + 1 if abs(prev - best_fit) < 1e-4 else 0
        prev = best_fit
        conv.append(best_fit)

    return best_sol, best_fit, conv

# ── Run ────────────────────────────────────────────────────────────
print("\nRunning AQHSO on hostel grid...")
print(f"  Grid: {GRID_W}×{GRID_H} blocks, {AREA_W}×{AREA_H}m")
print(f"  Placing {N_CAMERAS} cameras across {len(CANDIDATES)} candidates")

t0 = time.time()
best_idx, best_err, convergence = aqhso()
elapsed = time.time() - t0

cam_positions = CANDIDATES[best_idx]
print(f"\n  Completed in {elapsed:.2f}s")
print(f"  Best coverage error: {best_err:.3f}m")
for i, (x, y) in enumerate(cam_positions):
    bx, by = int(x // BLOCK_M), int(y // BLOCK_M)
    print(f"  Camera {i+1}: ({x:.0f}m, {y:.0f}m) → Block ({bx},{by})")

# ── Save JSON ──────────────────────────────────────────────────────
result = {
    "camera_positions": cam_positions.tolist(),
    "block_assignments": [
        {"cam_id": i, "x_m": float(x), "y_m": float(y),
         "block_x": int(x // BLOCK_M), "block_y": int(y // BLOCK_M),
         "zone_id": int(y // BLOCK_M) * GRID_W + int(x // BLOCK_M)}
        for i, (x, y) in enumerate(cam_positions)
    ],
    "coverage_error_m": float(best_err),
    "grid": {"width": GRID_W, "height": GRID_H, "block_size_m": BLOCK_M},
    "n_zones": GRID_W * GRID_H
}
with open("outputs/optimal_placements.json", "w") as f:
    json.dump(result, f, indent=2)

# ── Plot ───────────────────────────────────────────────────────────
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.5))
fig.patch.set_facecolor("white")

# Convergence curve
epochs = list(range(len(convergence)))
ax1.plot(epochs, convergence, color="#534AB7", linewidth=1.8)
ax1.fill_between(epochs, convergence, alpha=0.08, color="#534AB7")
ax1.axvline(int(0.2*len(convergence)), color="#888", ls="--", lw=0.9, label="Phase 1→2")
ax1.axvline(int(0.7*len(convergence)), color="#888", ls=":",  lw=0.9, label="Phase 2→3")
ax1.annotate(f"Final: {best_err:.3f}m",
             xy=(len(convergence)-1, best_err),
             xytext=(len(convergence)*0.5, convergence[0]*0.6),
             arrowprops=dict(arrowstyle="->", color="#534AB7"),
             color="#534AB7", fontsize=10, fontweight="bold")
ax1.set_xlabel("Epoch"); ax1.set_ylabel("Coverage Error (m)")
ax1.set_title("AQHSO Convergence — Hostel Grid", fontweight="bold")
ax1.legend(fontsize=9); ax1.grid(alpha=0.18)
ax1.spines[["top","right"]].set_visible(False)

# Hostel grid map
ax2.set_xlim(-3, AREA_W+3); ax2.set_ylim(-3, AREA_H+3)
ax2.set_aspect("equal")
for gy in range(GRID_H):
    for gx in range(GRID_W):
        r = mpatches.FancyBboxPatch(
            (gx*BLOCK_M+0.8, gy*BLOCK_M+0.8), BLOCK_M-1.6, BLOCK_M-1.6,
            boxstyle="round,pad=0.5", fc="#EEEDFE", ec="#AFA9EC", lw=0.8)
        ax2.add_patch(r)
        ax2.text(gx*BLOCK_M+BLOCK_M/2, gy*BLOCK_M+BLOCK_M/2,
                 f"B{gy*GRID_W+gx+1}", ha="center", va="center",
                 fontsize=9, color="#3C3489", fontweight="bold")

# Coverage circles
for (x, y) in cam_positions:
    ax2.add_patch(plt.Circle((x, y), 12, color="#1D9E75", alpha=0.13))

ax2.scatter(ANCHORS[:,0], ANCHORS[:,1], s=140, c="#D85A30",
            marker="s", zorder=4, label="Entry/Exit")
ax2.scatter(cam_positions[:,0], cam_positions[:,1], s=200,
            c="#534AB7", marker="^", zorder=5, label=f"Camera ×{N_CAMERAS}")
ax2.set_title("Optimal Camera Placement", fontweight="bold")
ax2.set_xlabel("Metres →"); ax2.set_ylabel("Metres →")
ax2.legend(fontsize=9); ax2.grid(alpha=0.15, ls="--")
ax2.spines[["top","right"]].set_visible(False)

plt.tight_layout()
plt.savefig("outputs/aqhso_convergence.png", dpi=180,
            bbox_inches="tight", facecolor="white")
print("\n  Saved: outputs/aqhso_convergence.png  ← PUT THIS ON YOUR SLIDES")
print("  Saved: outputs/optimal_placements.json")
print("\nPhase 0 complete. Run: python phase1/test_detection.py\n")
