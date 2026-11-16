"""Run ONLY the engine build in a fresh process and report peak RSS.

Invoked as a subprocess so the resident-set measurement reflects the
engine alone, not the test harness or the input-data generator.  A
background thread watches /proc/<pid>/fd for SQLite's on-disk temp
objects (this build names them ``etilqs_*``; they are unlinked right
after creation, so directory listings miss them but open fds do not).

Prints one JSON line with peak RSS, the result cell and spill evidence.
"""
import json
import os
import resource
import sqlite3
import sys
import threading
import time

sys.path.insert(0, "/workspace")

from rekey import engine  # noqa: E402


def _watch_temp_files(stop: dict, seen: set, poll: float = 0.01) -> None:
    pid = os.getpid()
    fd_dir = f"/proc/{pid}/fd"
    while not stop.get("stop"):
        try:
            for fd in os.listdir(fd_dir):
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if "etilqs" in target or "/ett" in target:
                    seen.add(target.split()[0])
        except FileNotFoundError:
            pass
        time.sleep(poll)


def main() -> int:
    inp, out, cache_mib, workdir = (
        sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4])
    start = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    seen: set = set()
    stop = {"stop": False}
    watcher = threading.Thread(
        target=_watch_temp_files, args=(stop, seen), daemon=True)
    watcher.start()
    engine.build(inp, out, cache_mib=cache_mib, workdir=workdir)
    time.sleep(0.1)
    stop["stop"] = True
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    con = sqlite3.connect(out)
    row = con.execute(
        "SELECT sum_amount,cnt_nonnull,min_amount,distinct_ticket "
        "FROM fact_cell_new").fetchone()
    n_lineage = con.execute("SELECT COUNT(*) FROM cell_lineage").fetchone()[0]
    con.close()
    print(json.dumps({
        "rss_start_kb": start,
        "rss_peak_kb": peak,
        "rss_growth_kb": peak - start,
        "cell": {"sum": row[0], "cnt": row[1], "min": row[2],
                 "distinct": row[3]},
        "lineage_rows": n_lineage,
        "disk_temp_objects": len(seen),
        "spilled_to_disk": len(seen) > 0,
    }))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
