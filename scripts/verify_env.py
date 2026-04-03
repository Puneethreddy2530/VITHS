"""
Phase 0 — verify_env.py
Run this first. Fix every FAIL before the hackathon.
Usage: python phase0/verify_env.py
"""
import sys, importlib

OK   = "\033[92m  [OK]\033[0m"
FAIL = "\033[91m  [FAIL]\033[0m"
WARN = "\033[93m  [WARN]\033[0m"

def check(label, fn):
    try:
        r = fn()
        print(f"{OK} {label}" + (f" — {r}" if r else ""))
        return True
    except Exception as e:
        print(f"{FAIL} {label} — {e}")
        return False

print("\n══════════════════════════════════════")
print("  PS-003 Environment Check")
print("══════════════════════════════════════\n")

check("Python >= 3.8", lambda: f"{sys.version_info.major}.{sys.version_info.minor}")

PKGS = [
    ("torch",               "PyTorch"),
    ("cv2",                 "OpenCV"),
    ("numpy",               "NumPy"),
    ("sklearn",             "scikit-learn"),
    ("ultralytics",         "YOLOv8"),
    ("transformers",        "HuggingFace Transformers"),
    ("faiss",               "FAISS"),
    ("fastapi",             "FastAPI"),
    ("uvicorn",             "Uvicorn"),
    ("dotenv",              "python-dotenv"),
    ("google.generativeai", "Gemini SDK"),
    ("PIL",                 "Pillow"),
    ("pyttsx3",             "pyttsx3 TTS (voice alerts)"),
]
print("── Packages ──────────────────────────")
for pkg, name in PKGS:
    check(name, lambda p=pkg: importlib.import_module(p) and None)

print("\n── CUDA ───────────────────────────────")
try:
    import torch
    if torch.cuda.is_available():
        dev = torch.cuda.get_device_name(0)
        mem = torch.cuda.get_device_properties(0).total_memory / 1e9
        print(f"{OK} CUDA — {dev} ({mem:.1f}GB)")
    else:
        print(f"{WARN} CUDA not available — CPU mode works fine")
except Exception as e:
    print(f"{FAIL} {e}")

print("\n── Models (pre-download) ──────────────")
def test_yolo():
    from ultralytics import YOLO
    YOLO("yolov8n.pt")
    return "yolov8n.pt ready"
check("YOLOv8-nano", test_yolo)

def test_clip():
    from transformers import CLIPProcessor, CLIPModel
    CLIPProcessor.from_pretrained("openai/clip-vit-base-patch32")
    CLIPModel.from_pretrained("openai/clip-vit-base-patch32")
    return "CLIP ready"
check("CLIP (openai/clip-vit-base-patch32)", test_clip)

print("\n── Webcam ─────────────────────────────")
def test_cam():
    import cv2
    cap = cv2.VideoCapture(0)
    ok  = cap.isOpened()
    cap.release()
    if not ok: raise Exception("No webcam found at index 0")
    return "webcam index 0 accessible"
check("Webcam", test_cam)

print("\n══════════════════════════════════════")
print("  Next: python phase0/aqhso_grid.py")
print("══════════════════════════════════════\n")
