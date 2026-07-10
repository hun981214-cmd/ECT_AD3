#!/usr/bin/env python3
"""Supervisor for long drift runs: restart the logger if its process dies.

The WaveForms library can segfault the whole Python process during degraded
USB episodes (observed 2026-07-10 after ~6 h: DptiIO ERC:7 storms followed by
SIGSEGV). In-process retry/recovery cannot survive that, so this watchdog
re-launches `ad3_timeseries_logger.py` with sequential run ids until the
total logging window is covered. Each segment is a normal, self-contained
run (own CSV/blocks/meta); stitch segments in analysis by run-id prefix.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-prefix", required=True, help="run ids become <prefix>_s<NN>")
    parser.add_argument("--total-hours", type=float, required=True)
    parser.add_argument("--interval-s", type=float, default=10.0)
    parser.add_argument("--start-segment", type=int, default=1)
    parser.add_argument("--restart-delay-s", type=float, default=15.0)
    parser.add_argument("--min-segment-s", type=float, default=120.0,
                        help="stop when the remaining window is shorter than this")
    parser.add_argument("logger_args", nargs="*",
                        help="extra args forwarded to ad3_timeseries_logger.py (prefix with --)")
    args = parser.parse_args()

    deadline = time.monotonic() + args.total_hours * 3600.0
    segment = args.start_segment
    while True:
        remaining = deadline - time.monotonic()
        if remaining < args.min_segment_s:
            print(f"[watchdog] window covered; stopping (remaining {remaining:.0f} s)")
            return 0
        run_id = f"{args.run_prefix}_s{segment:02d}"
        cmd = [
            sys.executable,
            str(SCRIPT_DIR / "ad3_timeseries_logger.py"),
            "--run-id", run_id,
            "--interval-s", str(args.interval_s),
            "--duration-s", str(int(remaining)),
            *args.logger_args,
        ]
        print(f"[watchdog] {datetime.now().isoformat(timespec='seconds')} "
              f"starting segment {run_id} for up to {remaining/3600:.2f} h")
        result = subprocess.run(cmd, check=False)
        if result.returncode == 0:
            print(f"[watchdog] segment {run_id} completed cleanly")
            return 0
        print(f"[watchdog] segment {run_id} exited with code {result.returncode}; "
              f"restarting after {args.restart_delay_s:.0f} s")
        segment += 1
        time.sleep(args.restart_delay_s)


if __name__ == "__main__":
    raise SystemExit(main())
