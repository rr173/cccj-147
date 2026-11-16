"""Independent naive re-key implementation for cross-checking.

This deliberately re-derives every result with plain Python dictionaries
and sets, reading the input tables directly.  It imports none of the
engine's SQL logic, so agreement between the two is a real cross-check.
Only suitable for small inputs -- the engine is the disk-scale path.

Handwritten expectations for the headline scenarios live separately in
``run_tests.py``; this enumerator is an additional independent oracle.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict


def naive_rekey(input_path: str) -> dict:
    con = sqlite3.connect(input_path)
    con.row_factory = sqlite3.Row
    versions = sorted(r[0] for r in con.execute(
        "SELECT DISTINCT version FROM mapping"))
    old_v, new_v = versions
    new_map = {(r["source"], r["member_id"]): r["customer"]
               for r in con.execute(
                   "SELECT * FROM mapping WHERE version=?", (new_v,))}
    shards = {r["shard_id"]: dict(r) for r in con.execute(
        "SELECT * FROM catalog_shard")}

    # cells[(customer, date, metric)] = aggregator state
    sums: dict = defaultdict(int)
    cnt: dict = defaultdict(int)
    mins: dict = {}
    tickets: dict = defaultdict(set)
    blocked_agg: set = set()
    blocked_dist: set = set()

    def add_amount(key, amount):
        if amount is None:
            mins.setdefault(key, None)
            return
        sums[key] += amount
        cnt[key] += 1
        mins[key] = amount if key not in mins or mins[key] is None \
            else min(mins[key], amount)

    def add_ticket(key, ticket):
        if ticket is not None:
            tickets[key].add(ticket)

    # atomic + full_summary: detailed, always rebuildable
    for r in con.execute("SELECT * FROM atomic_fact"):
        key = (new_map[(r["src_source"], r["member_id"])],
               r["date_key"], shards[r["shard_id"]]["metric"])
        add_amount(key, r["amount"])
        add_ticket(key, r["ticket"])
    for r in con.execute("SELECT * FROM summary_contribution"):
        key = (new_map[(r["member_source"], r["member_id"])],
               r["date_key"], shards[r["shard_id"]]["metric"])
        add_amount(key, r["amount"])
        add_ticket(key, r["ticket"])

    # old-summary cells
    count_only: dict = defaultdict(list)   # key -> [(shard, cnt)]
    for r in con.execute("SELECT * FROM old_summary_cell"):
        members = [tuple(x) for x in json.loads(r["participating_members"])]
        targets = {new_map[m] for m in members}
        key_base = (r["date_key"], r["metric"])
        exact = [x["ticket"] for x in con.execute(
            "SELECT ticket FROM old_summary_ticket_set WHERE shard_id=? "
            "AND old_customer=? AND date_key=? AND metric=?",
            (r["shard_id"], r["old_customer"], r["date_key"], r["metric"]))]
        if len(targets) > 1:
            for t in targets:
                k = (t,) + key_base
                blocked_agg.add(k)
                blocked_dist.add(k)
            continue
        k = (next(iter(targets)),) + key_base
        # carry sum/cnt/min wholesale
        if r["sum_amount"] is not None:
            sums[k] += r["sum_amount"]
        cnt[k] += r["cnt_nonnull"]
        if r["min_amount"] is not None:
            mins[k] = r["min_amount"] if k not in mins or mins[k] is None \
                else min(mins[k], r["min_amount"])
        else:
            mins.setdefault(k, None)
        if exact:
            for t in exact:
                tickets[k].add(t)
        elif r["distinct_ticket_cnt"] is not None:
            count_only[k].append((r["shard_id"], r["distinct_ticket_cnt"]))

    all_keys = set(sums) | set(mins) | set(tickets) | blocked_agg \
        | set(count_only)
    result = {}
    for k in all_keys:
        is_agg_blocked = k in blocked_agg
        # distinct verdict
        if k in blocked_dist:
            d_status, d_val = "blocked", None
        elif k in count_only and len(count_only[k]) == 1 \
                and k not in tickets:
            d_status, d_val = "carried", count_only[k][0][1]
        elif k in count_only:
            d_status, d_val = "blocked", None
        elif k in tickets:
            d_status, d_val = "rebuilt", len(tickets[k])
        else:
            d_status, d_val = "none", None
        result[k] = {
            "sum_amount": None if is_agg_blocked or cnt.get(k, 0) == 0
                           else sums.get(k, 0),
            "cnt_nonnull": None if is_agg_blocked else cnt.get(k, 0),
            "min_amount": None if is_agg_blocked else mins.get(k),
            "distinct_ticket": d_val,
            "agg_status": "blocked" if is_agg_blocked else "rebuilt",
            "distinct_status": d_status,
        }
    con.close()
    return result
