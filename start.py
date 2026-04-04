#!/usr/bin/env python3
"""
PS-003 — one command to run the API + dashboard.

    cd path\\to\\VITHS
    python start.py

Uses .venv\\Scripts\\python.exe when it exists and you are not already using it.
Optional PORT env (default 8888). Extra args are passed to uvicorn, e.g.:

    python start.py --reload
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def _venv_python() -> str:
    if sys.platform == "win32":
        return os.path.join(ROOT, ".venv", "Scripts", "python.exe")
    return os.path.join(ROOT, ".venv", "bin", "python")


def _same_executable(a: str, b: str) -> bool:
    try:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
    except OSError:
        return False


def main() -> None:
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)

    vpy = _venv_python()
    venv_exists = os.path.isfile(vpy)
    need_venv_subprocess = venv_exists and not _same_executable(sys.executable, vpy)
    extra = sys.argv[1:]
    port = int(os.environ.get("PORT", "8888"))

    env = os.environ.copy()
    sep = os.pathsep
    prev = env.get("PYTHONPATH", "").strip(sep)
    env["PYTHONPATH"] = ROOT + (sep + prev if prev else "")

    # Re-invoke with venv, or forward any CLI flags to uvicorn
    if need_venv_subprocess or extra:
        py = vpy if venv_exists else sys.executable
        cmd = [
            py,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "0.0.0.0",
            "--port",
            str(port),
            *extra,
        ]
        if need_venv_subprocess:
            print(f"Using virtualenv: {py}")
        elif venv_exists:
            print(f"Using virtualenv: {sys.executable}")
        else:
            print("[WARN] No .venv — using", py)
        print(f"PS-003 → http://127.0.0.1:{port}/")
        print("Press Ctrl+C to stop.\n")
        raise SystemExit(subprocess.call(cmd, cwd=ROOT, env=env))

    if not venv_exists:
        print("[WARN] No .venv found — using", sys.executable)
        print("        Create one:  python -m venv .venv")
    else:
        print(f"Using virtualenv: {sys.executable}")

    print(f"PS-003 → http://127.0.0.1:{port}/")
    print("Press Ctrl+C to stop.\n")

    import uvicorn

    uvicorn.run("backend.main:app", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
