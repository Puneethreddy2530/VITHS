"""
Phase 1 — test_detection.py
Runs detector on webcam. Press Q to quit.
This is your Phase 1 done check.

Usage: python phase1/test_detection.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2, time
from backend.engine.detector import Detector

def main():
    det = Detector()
    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("ERROR: Cannot open webcam")
        return

    cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    print("Webcam running. Press Q to quit.")
    print("Collecting warmup frames for IsolationForest (~150 frames)...\n")

    fps_times = []
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        t0     = time.time()
        result = det.process(frame, zone_id=0)
        fps_times.append(time.time() - t0)

        # Print anomaly events
        if result["is_anomaly"]:
            print(f"[ANOMALY] frame={result['frame']:5d}  "
                  f"clip={result['clip_score']:.3f}  "
                  f"ifor={result['iforest_score']:.3f}  "
                  f"reasons: {result['reasons']}")

        annotated = det.annotate(frame, result)

        # FPS display
        if len(fps_times) > 20:
            avg_ms = sum(fps_times[-20:]) / 20 * 1000
            cv2.putText(annotated, f"{avg_ms:.0f}ms/frame",
                        (annotated.shape[1]-130, 24),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200,200,200), 1)

        cv2.imshow("PS-003 Detection (Q to quit)", annotated)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()

    avg = sum(fps_times)/len(fps_times)*1000 if fps_times else 0
    print(f"\nAverage processing time: {avg:.1f}ms/frame")
    print("Phase 1 complete. Run: python phase2/test_pipeline.py")

if __name__ == "__main__":
    main()
