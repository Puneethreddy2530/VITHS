"""
Phase 2 — test_pipeline.py
Tests full pipeline with behavior labels, pattern IDs, heatmap.
Press Q to quit.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2, time
from backend.engine.pipeline import Pipeline

def main():
    pipeline = Pipeline()
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    print("Phase 2 pipeline running. Press Q to quit.\n")

    while True:
        ret, frame = cap.read()
        if not ret: break

        result = pipeline.process(frame, zone_id=5)  # test zone 5

        if result["is_anomaly"]:
            pat = result.get("pattern") or {}
            print(f"[ALERT] {result['behavior_label']}")
            print(f"        CLIP={result['clip_score']:.3f}  "
                  f"IFor={result['iforest_score']:.3f}  "
                  f"Night={result['thresholds']['night']}")
            if pat:
                print(f"        {pat['label']}")
            print()

        annotated = pipeline.annotate(frame, result)

        # Show behavior label on frame
        beh = result["behavior_label"]
        cv2.putText(annotated, beh, (10, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 200), 1)

        # Show pattern ID if any
        if result.get("pattern"):
            cv2.putText(annotated, result["pattern"]["label"],
                        (10, 82), cv2.FONT_HERSHEY_SIMPLEX,
                        0.45, (255, 220, 0), 1)

        cv2.imshow("PS-003 Phase 2 (Q to quit)", annotated)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()
    print("\nPhase 2 complete. Run: python phase3/test_memory.py")

if __name__ == "__main__":
    main()
