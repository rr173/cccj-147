"""Edge-case and stress input builders (independent of the engine)."""
from __future__ import annotations

from tests.scenarios import (
    add_atomic,
    add_catalog,
    add_full,
    add_mapping,
    add_old_cell,
    new_database,
)


# ---------------------------------------------------------------------------
# All-null amounts, empty-string ticket, equal-value distinct facts.
# ---------------------------------------------------------------------------
def build_null_and_empty_ticket(path: str) -> None:
    con = new_database(path)
    add_mapping(con, "v0", {("s1", "a"): "A", ("s1", "b"): "A"})
    add_mapping(con, "v1", {("s1", "a"): "N", ("s1", "b"): "N"})
    add_catalog(con, "sh_n", "atomic", "day", "m_null", "s1", 1, 3)
    add_atomic(con, "sh_n", "s1", 1, "a", "2026-02-01", None, None)
    add_atomic(con, "sh_n", "s1", 2, "b", "2026-02-01", None, "")
    # Two distinct facts with identical values: still two facts, one
    # distinct ticket object ('' counts once), cnt still 0.
    add_atomic(con, "sh_n", "s1", 3, "a", "2026-02-01", None, "")
    con.commit()
    con.close()


def build_equal_value_facts(path: str) -> None:
    con = new_database(path)
    add_mapping(con, "v0", {("s1", "a"): "A"})
    add_mapping(con, "v1", {("s1", "a"): "N"})
    add_catalog(con, "sh_e", "atomic", "day", "m_eq", "s1", 1, 2)
    add_atomic(con, "sh_e", "s1", 1, "a", "2026-03-01", 7, "Z", "p-1")
    add_atomic(con, "sh_e", "s1", 2, "a", "2026-03-01", 7, "Z", "p-2")
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Same member_id 'm' in two sources; they are different identities and must
# be routed via the composite (source, member_id).
# ---------------------------------------------------------------------------
def build_cross_source_member(path: str) -> None:
    con = new_database(path)
    add_mapping(con, "v0", {
        ("srcX", "m"): "A",
        ("srcY", "m"): "A",
    })
    add_mapping(con, "v1", {
        ("srcX", "m"): "P",
        ("srcY", "m"): "Q",
    })
    add_catalog(con, "sh_x", "atomic", "day", "m_x", "srcX", 1, 1)
    add_catalog(con, "sh_y", "atomic", "day", "m_y", "srcY", 1, 1)
    add_atomic(con, "sh_x", "srcX", 1, "m", "2026-04-01", 11)
    add_atomic(con, "sh_y", "srcY", 1, "m", "2026-04-01", 22)
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Partial recovery: one atomic shard is fully rebuildable, one old-summary
# shard splits and blocks.  The cell's sum must NOT present the computable
# part as the complete metric; the cell is blocked, but the lineage still
# shows the recoverable atomic contribution.
# ---------------------------------------------------------------------------
def build_partial_blocked(path: str) -> None:
    con = new_database(path)
    add_mapping(con, "v0", {
        ("s1", "a"): "A", ("s1", "b"): "A", ("s1", "c"): "C",
    })
    add_mapping(con, "v1", {
        ("s1", "a"): "P", ("s1", "b"): "Q", ("s1", "c"): "P",
    })
    # same metric 'sales', non-overlapping closed segments
    add_catalog(con, "sh_detail", "atomic", "day", "sales", "s1", 1, 1)
    add_catalog(con, "sh_old", "old_summary", "day", "sales", "s1", 10, 10)
    add_atomic(con, "sh_detail", "s1", 1, "c", "2026-05-01", 9)
    add_old_cell(con, "sh_old", "A", "2026-05-01", "sales",
                 30, 2, 10, None, [("s1", "a"), ("s1", "b")])
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Full-summary shard: member-level preserved contributions regrouped under
# the new mapping, including same-value distinct rows.
# ---------------------------------------------------------------------------
def build_full_summary(path: str) -> None:
    con = new_database(path)
    add_mapping(con, "v0", {("s1", "a"): "A", ("s1", "b"): "A"})
    add_mapping(con, "v1", {("s1", "a"): "P", ("s1", "b"): "Q"})
    add_catalog(con, "sh_full", "full_summary", "month", "m_full",
                "s1", 1, 4)
    add_full(con, "sh_full", "s1", "a", "2026-06", 10, "T", None, 1)
    add_full(con, "sh_full", "s1", "a", "2026-06", 10, "T", None, 2)
    add_full(con, "sh_full", "s1", "b", "2026-06", 20, None, None, 3)
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Catalog overlap: two shards of the same metric intersect on one source.
# The whole build must fail and leave no readable result.
# ---------------------------------------------------------------------------
def build_overlap(path: str) -> None:
    con = new_database(path)
    add_mapping(con, "v0", {("s1", "a"): "A"})
    add_mapping(con, "v1", {("s1", "a"): "P"})
    add_catalog(con, "sh_1", "atomic", "day", "m", "s1", 1, 10)
    add_catalog(con, "sh_2", "atomic", "day", "m", "s1", 5, 15)
    add_atomic(con, "sh_1", "s1", 1, "a", "2026-07-01", 1)
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Large file: many members / contributions to force disk spilling.  Only
# SQLite-side aggregates ever touch these rows.
# ---------------------------------------------------------------------------
def build_large(path: str, members: int = 40_000, rows_per_member: int = 50,
                ticket_pool: int = 5_000, batch: int = 10_000) -> dict:
    con = new_database(path)
    # Every member stays in old customer A and moves to new customer N.
    add_mapping(con, "v0", {("s1", f"m{i}"): "A" for i in range(members)})
    add_mapping(con, "v1", {("s1", f"m{i}"): "N" for i in range(members)})
    total = members * rows_per_member
    add_catalog(con, "sh_big", "atomic", "day", "m_big", "s1", 1, total)
    con.commit()
    # Batched inserts: the full row list is never held in Python at once.
    pending: list[tuple] = []
    seq = 0
    expected_sum = 0
    expected_min = None
    for i in range(members):
        member = f"m{i}"
        for j in range(rows_per_member):
            seq += 1
            amount = (i * 7 + j * 3) % 1_000
            ticket = f"TK{(i + j) % ticket_pool}"
            expected_sum += amount
            expected_min = amount if expected_min is None \
                else min(expected_min, amount)
            pending.append(("sh_big", "s1", seq, member, "2026-08-01",
                            amount, ticket, f"phys-{seq}"))
            if len(pending) >= batch:
                con.executemany(
                    "INSERT INTO atomic_fact VALUES(?,?,?,?,?,?,?,?)",
                    pending)
                pending.clear()
    if pending:
        con.executemany(
            "INSERT INTO atomic_fact VALUES(?,?,?,?,?,?,?,?)", pending)
    con.commit()
    con.close()
    return {"members": members, "rows": total,
            "expected_sum": expected_sum,
            "expected_cnt": total,
            "expected_min": expected_min,
            "expected_distinct": ticket_pool}
