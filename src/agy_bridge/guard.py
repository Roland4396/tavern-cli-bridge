"""Single-threaded process guardian; survives server crash long enough to reap.
Do not exec unshare: entering a user namespace clears parent-death handling.
"""
import ctypes
import os
import signal
import shutil
from pathlib import Path
import subprocess
import sys


def main():
    expected = int(sys.argv[1]); child = None; stopping = False
    def forward(signum, frame):
        nonlocal stopping
        stopping = True
        if child is not None:
            try: os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError: pass
    signal.signal(signal.SIGTERM, forward); signal.signal(signal.SIGINT, forward)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0: raise OSError(ctypes.get_errno(), 'prctl failed')
    if os.getppid() != expected or stopping: return 125
    try:
        child = subprocess.Popen(sys.argv[2:], start_new_session=True)
        while not stopping:
            try: return child.wait(timeout=.1)
            except subprocess.TimeoutExpired: pass
        forward(signal.SIGTERM, None)
        try: return child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL); return child.wait(timeout=1)
    finally:
        if child is not None and child.poll() is None:
            try: os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            child.wait(timeout=1)
        if os.getppid() != expected:
            path = Path(os.environ.get('AGY_BRIDGE_REQUEST_DIR', '/nonexistent'))
            if path.name.startswith('request-') and path.is_dir() and not path.is_symlink() and path.stat().st_uid == os.getuid():
                shutil.rmtree(path)

if __name__ == '__main__': raise SystemExit(main())
