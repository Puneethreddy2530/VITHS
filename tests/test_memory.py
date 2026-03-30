"""
Phase 3 — test_memory.py
Tests FAISS memory layer on webcam.
Simulates 5 anomaly events then tests recall.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2, time
from backend.engine.pipeline import Pipeline
from backend.engine.memory import EpisodicMemory

def main():
    pipeline = Pipeline()
    memory   = EpisodicMemory()
    cap      = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    print("Phase 3 memory test. Press Q to quit.\n")
    event_count = 0

    while True:
        ret, frame = cap.read()
        if not ret: break

        result = pipeline.process(frame, zone_id=3)

        if result["is_anomaly"]:
            # Process through memory layer
            enriched = memory.process(frame, result)
            event_count += 1

            print(f"[EVENT #{event_count}]")
            print(f"  Behavior:   {enriched['behavior_label']}")
            print(f"  Risk tier:  {enriched['risk_tier']}")
            print(f"  Recurrence: {enriched['recurrence']} similar past events")
            if enriched["similar_events"]:
                top = enriched["similar_events"][0]
                print(f"  Most similar: {top['event']['behavior']} "
                      f"(sim={top['similarity']:.3f}, "
                      f"at {top['event']['timestamp'][:19]})")
            print(f"  Total in memory: {enriched['total_stored']}")
            print()

        annotated = pipeline.annotate(frame, result)

        # Memory overlay
        cv2.putText(annotated, f"Memory: {memory._index.ntotal} events",
                    (10, 105), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (180,180,255), 1)

        cv2.imshow("PS-003 Phase 3 Memory (Q to quit)", annotated)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

    cap.release()
    cv2.destroyAllWindows()
    print(f"\nPhase 3 complete. {memory._index.ntotal} events stored.")
    print("Run: python phase4/test_reasoning.py")

if __name__ == "__main__":
    main()
