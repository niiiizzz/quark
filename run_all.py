"""
Lazarus Single-Command Runner (run_all.py).
Starts:
1. API (uvicorn running app.api.main:app on PORT 8000).
2. Supervisor (python -m app.supervisor).
The supervisor automatically manages and scales the worker processes.
"""

import asyncio
import os
import signal
import subprocess
import sys
import time

# Enforce selector event loop on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def main():
    print("=" * 60)
    print("Starting Lazarus Async Job Runner & Dashboard...")
    print("=" * 60)

    # 1. Start API process using our server launcher (sets SelectorEventLoop)
    api_cmd = [
        sys.executable,
        "-m",
        "app.api.server",
    ]
    api_proc = subprocess.Popen(api_cmd)
    print(f"[API] Started PID {api_proc.pid} at http://localhost:8000")

    # 2. Start Supervisor process
    sup_cmd = [sys.executable, "-m", "app.supervisor"]
    sup_proc = subprocess.Popen(sup_cmd)
    print(f"[Supervisor] Started PID {sup_proc.pid}")

    def handle_signal(sig, frame):
        print("\nStopping Lazarus services...")
        api_proc.terminate()
        sup_proc.terminate()
        api_proc.wait()
        sup_proc.wait()
        print("All processes stopped cleanly.")
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        while True:
            time.sleep(1)
            if api_proc.poll() is not None or sup_proc.poll() is not None:
                print("One of the core services exited. Terminating stack.")
                handle_signal(None, None)
    except KeyboardInterrupt:
        handle_signal(None, None)


if __name__ == "__main__":
    main()
