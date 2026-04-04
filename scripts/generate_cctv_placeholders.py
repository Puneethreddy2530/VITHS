"""
Write short looping-style placeholder MP4s into frontend/static/ for offline CCTV tiles.
Requires: opencv-python (see requirements.txt)
"""
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "frontend", "static")

SPECS = [
    ("cctv_corridor.mp4", "CAM 02 // CORRIDOR", (42, 44, 48)),
    ("cctv_gate.mp4", "CAM 03 // MAIN GATE", (40, 48, 42)),
    ("cctv_parking.mp4", "CAM 04 // PARKING", (38, 42, 50)),
]


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    w, h = 640, 360
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    fps = 15.0
    n_frames = 90

    for filename, label, bgr in SPECS:
        path = os.path.join(OUT_DIR, filename)
        writer = cv2.VideoWriter(path, fourcc, fps, (w, h))
        if not writer.isOpened():
            print(f"[ERR] Could not open VideoWriter for {path}", file=sys.stderr)
            sys.exit(1)
        for i in range(n_frames):
            frame = np.zeros((h, w, 3), dtype=np.uint8)
            frame[:] = bgr
            t = i / fps
            band = int(30 + 25 * np.sin(t * 2.1))
            cv2.rectangle(frame, (0, 0), (w, 28), (band, band // 2, band // 3), -1)
            cv2.putText(
                frame, label, (20, h // 2 - 10),
                cv2.FONT_HERSHEY_SIMPLEX, 0.85, (210, 210, 215), 2, cv2.LINE_AA,
            )
            cv2.putText(
                frame, "OFFLINE DEMO PLACEHOLDER", (20, h // 2 + 28),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (150, 155, 165), 1, cv2.LINE_AA,
            )
            writer.write(frame)
        writer.release()
        size = os.path.getsize(path)
        print(f"Wrote {path} ({size // 1024} KB)")


if __name__ == "__main__":
    main()
