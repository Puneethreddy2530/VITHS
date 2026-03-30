
# PS-003: AI Intrusion & Activity Monitoring System
## Phase-by-Phase Build Guide

---

## PHASE 0 — Setup (Do TONIGHT)
```bash
cd ps003
pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu118
pip install transformers ultralytics scikit-learn faiss-cpu
pip install fastapi uvicorn websockets sqlalchemy aiofiles python-multipart
pip install google-generativeai opencv-python pillow numpy
pip install pynacl  # for PQC layer

python phase0/verify_env.py      # check everything works
python phase0/aqhso_grid.py      # generates outputs/aqhso_convergence.png
```
✅ Done when: convergence.png exists, all checks pass

---

## PHASE 1 — Detection Pipeline (Hours 0–6)
```bash
python phase1/test_detection.py  # test on webcam live
```
✅ Done when: terminal prints anomaly scores on webcam feed

---

## PHASE 2 — AQHSO + Behavior + ST-GCN (Hours 6–10)
```bash
python phase2/test_pipeline.py   # full pipeline test
```
✅ Done when: behavior labels print per detection event

---

## PHASE 3 — Memory Layer (Hours 10–13)
```bash
python phase3/test_memory.py     # test FAISS store + recall
```
✅ Done when: similar past events retrieved per new anomaly

---

## PHASE 4 — Gemini Reasoning (Hours 13–15)
```bash
# add your key to phase4/reasoning.py  (GEMINI_API_KEY)
python phase4/test_reasoning.py
```
✅ Done when: risk verdict + predicted time prints

---

## PHASE 5 — Backend (Hours 15–20)
```bash
python phase5/main.py            # starts FastAPI on :8000
```
✅ Done when: http://localhost:8000/docs shows all endpoints

---

## PHASE 6 — Frontend (Hours 20–24)
```bash
cd frontend && npm install && npm run dev   # React on :5173
```
✅ Done when: heatmap updates live when anomaly fires

---

## FOLDER STRUCTURE
```
ps003/
  phase0/   verify_env.py · aqhso_grid.py
  phase1/   detector.py · test_detection.py
  phase2/   pipeline.py · behavior.py · stgcn.py
  phase3/   memory.py · test_memory.py
  phase4/   reasoning.py · test_reasoning.py
  phase5/   main.py · models.py · database.py
  phase6/   security.py
  frontend/ React + D3 dashboard
  outputs/  aqhso_convergence.png · optimal_placements.json
```
