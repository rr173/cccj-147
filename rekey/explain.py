"""Per-cell explanation against a completed result database."""
from __future__ import annotations

import sqlite3


def open_result(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def explain_cell(con: sqlite3.Connection, customer: str, date_key: str,
                 metric: str) -> str:
    cell = con.execute(
        "SELECT * FROM fact_cell_new WHERE new_customer=? AND date_key=? "
        "AND metric=?",
        (customer, date_key, metric),
    ).fetchone()
    if cell is None:
        return (f"no result cell for customer={customer!r} date={date_key!r} "
                f"metric={metric!r}")
    lines = [
        f"CELL customer={cell['new_customer']} date={cell['date_key']} "
        f"metric={cell['metric']}",
        f"  sum={cell['sum_amount']} cnt_nonnull={cell['cnt_nonnull']} "
        f"min={cell['min_amount']} distinct_ticket="
        f"{cell['distinct_ticket']}",
        f"  agg_status={cell['agg_status']} "
        f"distinct_status={cell['distinct_status']}",
    ]

    reasons = con.execute(
        "SELECT part, shard_id, reason FROM blocked_reason "
        "WHERE new_customer=? AND date_key=? AND metric=? ORDER BY part, shard_id",
        (customer, date_key, metric),
    ).fetchall()
    if reasons:
        lines.append("  blocked / missing information:")
        for r in reasons:
            lines.append(f"    [{r['part']}] shard={r['shard_id']}: "
                         f"{r['reason']}")

    lineage = con.execute(
        "SELECT * FROM cell_lineage WHERE new_customer=? AND date_key=? "
        "AND metric=? ORDER BY shard_id, lineage_kind, source, seq, member_id",
        (customer, date_key, metric),
    ).fetchall()
    lines.append(f"  lineage ({len(lineage)} entries):")
    for r in lineage:
        ident = ""
        if r["lineage_kind"] in ("atomic", "full_contrib"):
            ident = (f" member=({r['source']},{r['member_id']})"
                     + (f" seq={r['seq']}" if r["seq"] is not None else "")
                     + f" amount={r['amount']} ticket={r['ticket']!r}")
        else:
            ident = f" old_customer={r['old_customer']}"
        lines.append(
            f"    - {r['lineage_kind']} shard={r['shard_id']}{ident}"
        )
        if r["note"]:
            lines.append(f"        note: {r['note']}")

    tickets = con.execute(
        "SELECT COUNT(*) c FROM cell_ticket_set WHERE new_customer=? "
        "AND date_key=? AND metric=?",
        (customer, date_key, metric),
    ).fetchone()["c"]
    lines.append(f"  exact surviving ticket set size kept in database: "
                 f"{tickets}")
    return "\n".join(lines)


def list_cells(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM fact_cell_new "
        "ORDER BY new_customer, date_key, metric"
    ).fetchall()


def shard_status(con: sqlite3.Connection) -> list[sqlite3.Row]:
    return con.execute(
        "SELECT * FROM metric_shard_status ORDER BY metric, shard_id"
    ).fetchall()
