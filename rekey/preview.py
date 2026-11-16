"""Re-key preview: run the whole pipeline into a throwaway database.

A preview never leaves a completed result behind.  It returns the same
row content a ``build`` would produce plus the blocked-reason list, so a
caller can review what would happen before committing an output path.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

from . import engine


def preview(input_path: str, workdir: str | None = None) -> dict:
    workdir = workdir or tempfile.gettempdir()
    fd, tmp = tempfile.mkstemp(prefix=".rekey-preview-", suffix=".db",
                               dir=workdir)
    os.close(fd)
    os.unlink(tmp)
    try:
        engine.build(input_path, tmp, workdir=workdir)
        con = sqlite3.connect(tmp)
        con.row_factory = sqlite3.Row
        try:
            cells = [dict(r) for r in con.execute(
                "SELECT * FROM fact_cell_new "
                "ORDER BY new_customer, date_key, metric")]
            blocked = [dict(r) for r in con.execute(
                "SELECT * FROM blocked_reason ORDER BY metric, part")]
            status = [dict(r) for r in con.execute(
                "SELECT * FROM metric_shard_status ORDER BY metric, shard_id")]
            meta = dict(con.execute("SELECT key, value FROM run_meta").fetchall())
        finally:
            con.close()
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
        for suffix in ("-wal", "-shm"):
            stray = tmp + suffix
            if os.path.exists(stray):
                os.unlink(stray)
    return {"cells": cells, "blocked": blocked, "shard_status": status,
            "meta": meta}


def render_report(result: dict) -> str:
    lines = ["=== re-key preview ==="]
    lines.append(f"mapping versions: {result['meta'].get('mapping_old_version')}"
                 f" -> {result['meta'].get('mapping_new_version')}")
    lines.append("")
    lines.append("new customer-grain cells:")
    header = (f"  {'customer':<10}{'date':<12}{'metric':<10}"
              f"{'sum':>6}{'cnt':>5}{'min':>6}{'distinct':>9}  "
              f"agg / distinct")
    lines.append(header)
    for c in result["cells"]:
        lines.append(
            f"  {c['new_customer']:<10}{c['date_key']:<12}{c['metric']:<10}"
            f"{str(c['sum_amount']):>6}{str(c['cnt_nonnull']):>5}"
            f"{str(c['min_amount']):>6}{str(c['distinct_ticket']):>9}  "
            f"{c['agg_status']} / {c['distinct_status']}"
        )
    if result["blocked"]:
        lines.append("")
        lines.append("blocked parts and missing information:")
        for b in result["blocked"]:
            lines.append(
                f"  [{b['part']}] {b['new_customer']}/{b['date_key']}/"
                f"{b['metric']} shard={b['shard_id']}: {b['reason']}"
            )
    lines.append("")
    lines.append("metric x shard recovery status:")
    for s in result["shard_status"]:
        lines.append(
            f"  {s['metric']:<10}{s['shard_id']:<10}{s['form']:<13}"
            f"agg={s['agg_status']:<9} distinct={s['distinct_status']:<9}"
            f"{s['detail']}"
        )
    return "\n".join(lines)
