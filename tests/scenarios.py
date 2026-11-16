"""Input-database builders for the test scenarios.

This module is deliberately independent of the re-key engine: it only
writes valid (and, on request, invalid) input SQLite files using plain
sqlite3 DDL.  Expected numbers in the test suite are hand-written in
``run_tests.py``, never produced by the engine.
"""
from __future__ import annotations

import json
import os
import sqlite3

from rekey.schema import INPUT_DDL


def new_database(path: str) -> sqlite3.Connection:
    if os.path.exists(path):
        os.unlink(path)
    con = sqlite3.connect(path)
    con.executescript(INPUT_DDL)
    return con


def add_mapping(con: sqlite3.Connection, version: str,
                assignments: dict[tuple[str, str], str]) -> None:
    con.executemany(
        "INSERT INTO mapping(version, source, member_id, customer) "
        "VALUES(?,?,?,?)",
        [(version, s, m, c) for (s, m), c in sorted(assignments.items())],
    )


def add_catalog(con: sqlite3.Connection, shard_id: str, form: str,
                grain: str, metric: str, source: str,
                seq_start: int, seq_end: int) -> None:
    con.execute(
        "INSERT INTO catalog_shard VALUES(?,?,?,?,?,?,?)",
        (shard_id, form, grain, metric, source, seq_start, seq_end),
    )


def add_atomic(con: sqlite3.Connection, shard_id: str, source: str,
               seq: int, member_id: str, date_key: str, amount,
               ticket=None, physical_id=None) -> None:
    con.execute(
        "INSERT INTO atomic_fact VALUES(?,?,?,?,?,?,?,?)",
        (shard_id, source, seq, member_id, date_key, amount, ticket,
         physical_id),
    )


def add_full(con: sqlite3.Connection, shard_id: str, member_source: str,
             member_id: str, date_key: str, amount, ticket,
             physical_id, row_idx: int) -> None:
    con.execute(
        "INSERT INTO summary_contribution VALUES(?,?,?,?,?,?,?,?)",
        (shard_id, member_source, member_id, date_key, amount, ticket,
         physical_id, row_idx),
    )


def add_old_cell(con: sqlite3.Connection, shard_id: str, old_customer: str,
                 date_key: str, metric: str, sum_amount, cnt_nonnull,
                 min_amount, distinct_ticket_cnt,
                 members: list[tuple[str, str]]) -> None:
    con.execute(
        "INSERT INTO old_summary_cell VALUES(?,?,?,?,?,?,?,?,?)",
        (shard_id, old_customer, date_key, metric, sum_amount, cnt_nonnull,
         min_amount, distinct_ticket_cnt, json.dumps(members)),
    )


def add_ticket_set(con: sqlite3.Connection, shard_id: str, old_customer: str,
                   date_key: str, metric: str, tickets: list[str]) -> None:
    con.executemany(
        "INSERT INTO old_summary_ticket_set VALUES(?,?,?,?,?)",
        [(shard_id, old_customer, date_key, metric, t) for t in tickets],
    )


# ---------------------------------------------------------------------------
# Scenario 1/2: customer A (members a,b) split into P/Q, then merged back.
# Detailed atomic facts: amounts 10 and 20, both ticket T.
# ---------------------------------------------------------------------------
def build_atomic_split(path: str) -> None:
    con = new_database(path)
    members = {("s1", "a"), ("s1", "b")}
    add_mapping(con, "v0", {m: "A" for m in members})
    add_mapping(con, "v1", {("s1", "a"): "P", ("s1", "b"): "Q"})
    add_catalog(con, "sh_atomic", "atomic", "day", "sales", "s1", 1, 2)
    add_atomic(con, "sh_atomic", "s1", 1, "a", "2026-01-01", 10, "T", "phys-1")
    add_atomic(con, "sh_atomic", "s1", 2, "b", "2026-01-01", 20, "T", "phys-2")
    con.commit()
    con.close()


def build_atomic_merge(path: str) -> None:
    con = new_database(path)
    # old version already carries the split customers P/Q ...
    add_mapping(con, "v0", {("s1", "a"): "P", ("s1", "b"): "Q"})
    # ... the approved new version merges both members into customer R.
    add_mapping(con, "v1", {("s1", "a"): "R", ("s1", "b"): "R"})
    add_catalog(con, "sh_atomic", "atomic", "day", "sales", "s1", 1, 2)
    add_atomic(con, "sh_atomic", "s1", 1, "a", "2026-01-01", 10, "T", "phys-1")
    add_atomic(con, "sh_atomic", "s1", 2, "b", "2026-01-01", 20, "T", "phys-2")
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Scenario 3: only an old-summary cell for A (sum 30, members {a,b}), no
# detail.  Splitting must block; 15/15 must never appear.
# ---------------------------------------------------------------------------
def build_oldsum_split(path: str) -> None:
    con = new_database(path)
    members = {("s1", "a"), ("s1", "b")}
    add_mapping(con, "v0", {m: "A" for m in members})
    add_mapping(con, "v1", {("s1", "a"): "P", ("s1", "b"): "Q"})
    add_catalog(con, "sh_old", "old_summary", "day", "sales", "s1", 1, 2)
    add_old_cell(con, "sh_old", "A", "2026-01-01", "sales",
                 30, 2, 10, 1, [("s1", "a"), ("s1", "b")])
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Scenario 4: old summaries C and D merge.  Without ticket sets the sum is
# usable (10) but the distinct count is blocked.  With both exact sets {X}
# the distinct number rebuilds to 1.
# ---------------------------------------------------------------------------
def build_cd_merge(path: str, with_sets: bool) -> None:
    con = new_database(path)
    add_mapping(con, "v0", {("s1", "c"): "C", ("s1", "d"): "D"})
    add_mapping(con, "v1", {("s1", "c"): "M", ("s1", "d"): "M"})
    add_catalog(con, "sh_c", "old_summary", "day", "sales", "s1", 1, 1)
    add_catalog(con, "sh_d", "old_summary", "day", "sales", "s1", 2, 2)
    add_old_cell(con, "sh_c", "C", "2026-01-01", "sales",
                 5, 1, 5, 1, [("s1", "c")])
    add_old_cell(con, "sh_d", "D", "2026-01-01", "sales",
                 5, 1, 5, 1, [("s1", "d")])
    if with_sets:
        add_ticket_set(con, "sh_c", "C", "2026-01-01", "sales", ["X"])
        add_ticket_set(con, "sh_d", "D", "2026-01-01", "sales", ["X"])
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Information-insufficiency proof.  Two raw datasets produce the SAME
# old-summary cell for A (sum 30, members a,b) but require DIFFERENT split
# answers.  Here they are stored as atomic datasets; the walkthrough test
# also derives the identical old-summary representation from both.
# ---------------------------------------------------------------------------
def build_ambiguous_raw(path: str, variant: int) -> None:
    con = new_database(path)
    members = {("s1", "a"), ("s1", "b")}
    add_mapping(con, "v0", {m: "A" for m in members})
    add_mapping(con, "v1", {("s1", "a"): "P", ("s1", "b"): "Q"})
    add_catalog(con, "sh_atomic", "atomic", "day", "sales", "s1", 1, 2)
    if variant == 1:
        # Raw split answer: P gets 10, Q gets 20.
        add_atomic(con, "sh_atomic", "s1", 1, "a", "2026-01-01", 10)
        add_atomic(con, "sh_atomic", "s1", 2, "b", "2026-01-01", 20)
    else:
        # Same retained old-summary triple (sum 30, cnt 2, min 10) but the
        # per-member truth is reversed: a holds 20 and b holds 10.  No
        # customer-grain summary can tell this variant from variant 1.
        add_atomic(con, "sh_atomic", "s1", 1, "a", "2026-01-01", 20)
        add_atomic(con, "sh_atomic", "s1", 2, "b", "2026-01-01", 10)
    con.commit()
    con.close()
