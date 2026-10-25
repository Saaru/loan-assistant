"""Starts the simulated core systems and the assistant together.

    python -m scripts.run

Re-seeds the demo data when it was generated for a different business date, so
"this month's EMI" always means this month.
"""

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

APP_PORT = os.getenv("LA_APP_PORT", "8000")
MOCK_PORT = os.getenv("LA_MOCK_PORT", "8100")
# Several worker processes so the simulated systems are not the bottleneck under load.
MOCK_WORKERS = os.getenv("LA_MOCK_WORKERS", "4")


def wait_for(url: str, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except Exception:
            time.sleep(0.3)
    return False


def main():
    from finlib.dates import business_date
    from mock_systems import seed

    if not seed.is_fresh():
        print(f"Seeding demo data for {business_date()}...")
        seed.seed()

    env = {**os.environ, "LA_MOCK_BASE_URL": f"http://127.0.0.1:{MOCK_PORT}"}
    uvicorn = [sys.executable, "-m", "uvicorn"]
    procs = [subprocess.Popen(uvicorn + ["mock_systems.app:app", "--port", MOCK_PORT, "--workers", MOCK_WORKERS,
                                         "--log-level", "warning"],
                              cwd=ROOT, env=env)]
    try:
        if not wait_for(f"http://127.0.0.1:{MOCK_PORT}/_admin/meta"):
            raise SystemExit("Core systems did not start")
        procs.append(subprocess.Popen(uvicorn + ["app.main:app", "--port", APP_PORT, "--log-level", "warning"],
                                      cwd=ROOT, env=env))
        if not wait_for(f"http://127.0.0.1:{APP_PORT}/api/health"):
            raise SystemExit("Assistant did not start")
        print(f"\n  Core systems  http://127.0.0.1:{MOCK_PORT}/docs\n  Assistant     http://127.0.0.1:{APP_PORT}\n")
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
