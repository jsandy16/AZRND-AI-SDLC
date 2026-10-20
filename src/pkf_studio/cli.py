"""`pkf studio [REPO]` starts the app on 127.0.0.1; `pkf studio verify --pr N` is the pkf-gate check."""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["verify"]:
        from .verify import main as verify

        return verify(argv[1:])
    ap = argparse.ArgumentParser(prog="pkf studio", description="pkf Studio: requirement → approved, built, locally-run pipeline")
    ap.add_argument("repo", nargs="?", default=".", help="a clone of the specs repository (default: here)")
    ap.add_argument("--port", type=int, default=8501)
    a = ap.parse_args(argv)
    try:
        import streamlit  # noqa: F401
    except ImportError:
        print("pkf studio needs the studio extra: pip install -e '.[runtime,agent,studio]'")
        return 2
    app = Path(__file__).with_name("app.py")
    env = {**os.environ, "PKF_STUDIO_ROOT": str(Path(a.repo).resolve()), "PYTHONUTF8": "1"}
    cmd = [sys.executable, "-m", "streamlit", "run", str(app), "--server.address", "127.0.0.1", "--server.port", str(a.port),
           "--server.headless", "true", "--server.fileWatcherType", "none", "--browser.gatherUsageStats", "false",
           "--server.enableXsrfProtection", "true", "--client.toolbarMode", "viewer", "--theme.base", "light", "--theme.primaryColor", "#2457D6", "--server.maxUploadSize", "10", "--global.developmentMode", "false"]
    print(f"pkf Studio on http://127.0.0.1:{a.port}  (Ctrl+C stops the app; running jobs keep going)")
    try:
        return subprocess.call(cmd, env=env)
    except KeyboardInterrupt:
        return 0
