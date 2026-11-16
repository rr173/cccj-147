"""Catalog and input validation.

The tool attaches the input database read-only (``mode=ro``) and never
mutates it.  Every check returns a list of human-readable error strings;
an empty list means the input is accepted.
"""
from __future__ import annotations

import json
import re
import sqlite3

from .schema import DATE_GRAIN

REQUIRED_TABLES = [
    "mapping",
    "catalog_shard",
    "atomic_fact",
    "summary_contribution",
    "old_summary_cell",
    "old_summary_ticket_set",
]


def open_input(path: str) -> sqlite3.Connection:
    """Open the input database strictly read-only via a file URI."""
    uri = "file:" + path + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only = ON")
    return con


def _q(con: sqlite3.Connection, sql: str, params=()):
    return con.execute(sql, params).fetchall()


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return bool(
        _q(
            con,
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        )
    )


def _check_tables(con: sqlite3.Connection) -> list[str]:
    errs: list[str] = []
    for t in REQUIRED_TABLES:
        if not _table_exists(con, t):
            errs.append(f"missing required table: {t}")
    return errs


def _check_mapping(con: sqlite3.Connection) -> list[str]:
    errs: list[str] = []
    versions = [r[0] for r in _q(con, "SELECT DISTINCT version FROM mapping")]
    if len(versions) != 2:
        errs.append(
            f"mapping must contain exactly two versions, found: {versions!r}"
        )
        return errs
    # Each member maps to exactly one customer inside a version (PK already
    # guarantees uniqueness of (version, source, member_id)).
    for v in versions:
        nulls = _q(
            con,
            "SELECT COUNT(*) FROM mapping WHERE version=? "
            "AND (source IS NULL OR member_id IS NULL OR customer IS NULL)",
            (v,),
        )[0][0]
        if nulls:
            errs.append(f"mapping version {v!r} contains NULL identity fields")
    a, b = versions
    rows_a = _q(
        con,
        "SELECT source, member_id FROM mapping WHERE version=? ORDER BY 1,2",
        (a,),
    )
    rows_b = _q(
        con,
        "SELECT source, member_id FROM mapping WHERE version=? ORDER BY 1,2",
        (b,),
    )
    set_a = {(r[0], r[1]) for r in rows_a}
    set_b = {(r[0], r[1]) for r in rows_b}
    if set_a != set_b:
        only_a, only_b = sorted(set_a - set_b), sorted(set_b - set_a)
        errs.append(
            "the two mapping versions do not share the same member universe; "
            f"only in {a!r}: {only_a[:5]} only in {b!r}: {only_b[:5]}"
        )
    return errs


def _check_catalog(con: sqlite3.Connection) -> list[str]:
    errs: list[str] = []
    if _q(con, "SELECT COUNT(*) FROM catalog_shard")[0][0] == 0:
        return ["catalog_shard is empty"]
    bad = _q(
        con,
        "SELECT shard_id FROM catalog_shard WHERE "
        "form NOT IN ('atomic','full_summary','old_summary') "
        "OR date_grain NOT IN ('day','month','year') "
        "OR metric IS NULL OR metric='' "
        "OR src_source IS NULL OR src_source='' "
        "OR seq_start IS NULL OR seq_end IS NULL OR seq_end < seq_start",
    )
    errs.extend(f"invalid catalog row / shard {r[0]!r}" for r in bad)
    dup = _q(
        con,
        "SELECT shard_id, COUNT(*) c FROM catalog_shard GROUP BY shard_id "
        "HAVING c > 1",
    )
    errs.extend(f"duplicate shard_id {r[0]!r}" for r in dup)

    # Closed coverage segments of different shards for the SAME metric must
    # not intersect.  Segments never cross sources, so overlap is judged
    # within one source.  Adjacent segments (end+1 = next start) are fine.
    overlaps = _q(
        con,
        """
        SELECT x.metric AS metric,
               x.shard_id AS shard_x, y.shard_id AS shard_y,
               x.src_source AS src,
               x.seq_start AS x_start, x.seq_end AS x_end,
               y.seq_start AS y_start, y.seq_end AS y_end
        FROM catalog_shard x
        JOIN catalog_shard y
          ON x.metric = y.metric
         AND x.src_source = y.src_source
         AND x.shard_id < y.shard_id
         AND x.seq_start <= y.seq_end
         AND y.seq_start <= x.seq_end
        """,
    )
    for r in overlaps:
        errs.append(
            f"coverage conflict for metric {r['metric']!r}: shards "
            f"{r['shard_x']!r} [{r['x_start']},{r['x_end']}] and "
            f"{r['shard_y']!r} [{r['y_start']},{r['y_end']}] overlap on "
            f"source {r['src']!r}"
        )
    return errs


def _check_date_shapes(con: sqlite3.Connection) -> list[str]:
    errs: list[str] = []
    grains = {
        r[0]: DATE_GRAIN[r[0]][0]
        for r in _q(con, "SELECT DISTINCT date_grain FROM catalog_shard")
        if r[0] in DATE_GRAIN
    }
    for table, col, shard_join in (
        ("atomic_fact", "date_key", "JOIN catalog_shard c USING(shard_id)"),
        (
            "summary_contribution",
            "date_key",
            "JOIN catalog_shard c USING(shard_id)",
        ),
        (
            "old_summary_cell",
            "date_key",
            "JOIN catalog_shard c USING(shard_id)",
        ),
    ):
        if not _table_exists(con, table):
            continue
        rows = _q(
            con,
            f"SELECT DISTINCT t.{col}, c.date_grain FROM {table} t "
            f"{shard_join}",
        )
        for date_key, grain in rows:
            if grain not in grains or not re.match(grains[grain], str(date_key)):
                errs.append(
                    f"{table}: date_key {date_key!r} does not match shard "
                    f"grain {grain!r} (expected {DATE_GRAIN[grain][1]})"
                )
    return errs


def _check_atomic(con: sqlite3.Connection) -> list[str]:
    errs: list[str] = []
    # atomic_fact rows may only live in atomic shards.
    bad_form = _q(
        con,
        "SELECT a.shard_id FROM atomic_fact a JOIN catalog_shard c "
        "ON a.shard_id=c.shard_id WHERE c.form <> 'atomic'",
    )
    errs.extend(
        f"atomic_fact row references non-atomic shard {r[0]!r}" for r in bad_form
    )
    unknown = _q(
        con,
        "SELECT a.shard_id FROM atomic_fact a LEFT JOIN catalog_shard c "
        "ON a.shard_id=c.shard_id WHERE c.shard_id IS NULL"
    )
    errs.extend(f"atomic_fact references unknown shard {r[0]!r}" for r in unknown)
    # (source, seq) must be inside the shard's closed segment; gaps allowed.
    outside = _q(
        con,
        """
        SELECT a.shard_id, a.src_source, a.seq
        FROM atomic_fact a JOIN catalog_shard c ON a.shard_id = c.shard_id
        WHERE a.src_source <> c.src_source
           OR a.seq < c.seq_start OR a.seq > c.seq_end
        """,
    )
    errs.extend(
        f"atomic contribution ({r[1]!r}, seq {r[2]}) lies outside shard "
        f"{r[0]!r} coverage segment"
        for r in outside
    )
    # Integer amounts only (SQLite dynamic typing: reject floats/text).
    nonint = _q(
        con,
        "SELECT shard_id, src_source, seq FROM atomic_fact "
        "WHERE amount IS NOT NULL AND typeof(amount) <> 'integer'"
    )
    errs.extend(
        f"amount must be INTEGER or NULL at ({r[0]!r},{r[1]!r},seq {r[2]})"
        for r in nonint
    )
    # Every member identity must exist in the mapping universe.
    bad_member = _q(
        con,
        """
        SELECT DISTINCT a.shard_id, a.src_source, a.member_id
        FROM atomic_fact a
        LEFT JOIN mapping m
          ON m.source = a.src_source AND m.member_id = a.member_id
        WHERE m.member_id IS NULL
        """,
    )
    errs.extend(
        f"atomic fact member ({r[1]!r},{r[2]!r}) is absent from the mapping"
        for r in bad_member
    )
    return errs


def _check_full_summary(con: sqlite3.Connection) -> list[str]:
    errs: list[str] = []
    bad_form = _q(
        con,
        "SELECT DISTINCT s.shard_id FROM summary_contribution s "
        "JOIN catalog_shard c ON s.shard_id=c.shard_id "
        "WHERE c.form <> 'full_summary'"
    )
    errs.extend(
        f"summary_contribution row in non-full_summary shard {r[0]!r}"
        for r in bad_form
    )
    unknown = _q(
        con,
        "SELECT DISTINCT s.shard_id FROM summary_contribution s "
        "LEFT JOIN catalog_shard c ON s.shard_id=c.shard_id "
        "WHERE c.shard_id IS NULL"
    )
    errs.extend(
        f"summary_contribution references unknown shard {r[0]!r}"
        for r in unknown
    )
    nonint = _q(
        con,
        "SELECT shard_id, member_source, member_id, row_idx "
        "FROM summary_contribution WHERE amount IS NOT NULL "
        "AND typeof(amount) <> 'integer'"
    )
    errs.extend(
        f"non-integer amount in full_summary shard {r[0]!r} member "
        f"({r[1]!r},{r[2]!r}) row {r[3]}"
        for r in nonint
    )
    bad_member = _q(
        con,
        """
        SELECT DISTINCT s.shard_id, s.member_source, s.member_id
        FROM summary_contribution s
        LEFT JOIN mapping m
          ON m.source = s.member_source AND m.member_id = s.member_id
        WHERE m.member_id IS NULL
        """
    )
    errs.extend(
        f"full_summary member ({r[1]!r},{r[2]!r}) absent from mapping"
        for r in bad_member
    )
    return errs


def _check_old_summary(con: sqlite3.Connection) -> list[str]:
    errs: list[str] = []
    bad_form = _q(
        con,
        "SELECT DISTINCT o.shard_id FROM old_summary_cell o "
        "JOIN catalog_shard c ON o.shard_id=c.shard_id "
        "WHERE c.form <> 'old_summary'"
    )
    errs.extend(
        f"old_summary_cell row in non-old_summary shard {r[0]!r}"
        for r in bad_form
    )
    unknown = _q(
        con,
        "SELECT DISTINCT o.shard_id FROM old_summary_cell o "
        "LEFT JOIN catalog_shard c ON o.shard_id=c.shard_id "
        "WHERE c.shard_id IS NULL"
    )
    errs.extend(
        f"old_summary_cell references unknown shard {r[0]!r}" for r in unknown
    )

    # Aggregate sanity: min/cnt consistency, integer types.
    bad = _q(
        con,
        """
        SELECT shard_id, old_customer, date_key, metric
        FROM old_summary_cell
        WHERE cnt_nonnull < 0
           OR (cnt_nonnull = 0 AND min_amount IS NOT NULL)
           OR (cnt_nonnull > 0 AND min_amount IS NULL)
           OR (sum_amount IS NOT NULL AND typeof(sum_amount) <> 'integer')
           OR (min_amount IS NOT NULL AND typeof(min_amount) <> 'integer')
           OR typeof(cnt_nonnull) <> 'integer'
           OR (distinct_ticket_cnt IS NOT NULL
               AND typeof(distinct_ticket_cnt) <> 'integer')
           OR (distinct_ticket_cnt IS NOT NULL AND distinct_ticket_cnt < 0)
        """
    )
    errs.extend(
        f"inconsistent old_summary cell {tuple(r)!r} (cnt/min/type mismatch)"
        for r in bad
    )

    # Participating member sets: parseable JSON, non-empty, known members,
    # all belonging to the stated old customer in the old mapping.
    versions = [r[0] for r in _q(con, "SELECT DISTINCT version FROM mapping")]
    old_version = min(versions) if versions else None
    parsed_problems: list[tuple] = []
    member_buckets: dict[tuple, list[tuple]] = {}
    for row in _q(
        con,
        "SELECT shard_id, old_customer, date_key, metric, "
        "participating_members FROM old_summary_cell",
    ):
        key = (row[0], row[1], row[2], row[3])
        try:
            members = json.loads(row[4])
            assert isinstance(members, list)
            for item in members:
                assert (
                    isinstance(item, list)
                    and len(item) == 2
                    and all(isinstance(x, str) for x in item)
                )
        except (ValueError, AssertionError, TypeError):
            parsed_problems.append(key)
            continue
        if not members:
            parsed_problems.append(key + ("empty participating set",))
            continue
        member_buckets[key] = [(a, b) for a, b in members]
    errs.extend(
        f"participating_members of {k!r} is not a non-empty JSON "
        "[[source, member_id], ...] array"
        for k in parsed_problems
    )

    known = {
        (r[0], r[1], r[2])
        for r in _q(
            con,
            "SELECT version, source, member_id FROM mapping",
        )
    }
    for key, members in member_buckets.items():
        shard_id, old_customer, _date, _metric = key
        for ident in members:
            if (old_version, ident[0], ident[1]) not in known:
                errs.append(
                    f"old_summary cell {key!r} names unknown member {ident!r}"
                )
            else:
                owner = _q(
                    con,
                    "SELECT customer FROM mapping WHERE version=? "
                    "AND source=? AND member_id=?",
                    (old_version, ident[0], ident[1]),
                )[0][0]
                if owner != old_customer:
                    errs.append(
                        f"old_summary cell {key!r}: member {ident!r} belongs "
                        f"to old customer {owner!r}, not {old_customer!r}"
                    )

    # Ticket sets may only accompany old_summary cells and must be consistent
    # with the stated distinct count when one is given.
    orphan = _q(
        con,
        """
        SELECT DISTINCT t.shard_id, t.old_customer, t.date_key, t.metric
        FROM old_summary_ticket_set t
        LEFT JOIN old_summary_cell o
          ON t.shard_id=o.shard_id AND t.old_customer=o.old_customer
         AND t.date_key=o.date_key AND t.metric=o.metric
        WHERE o.shard_id IS NULL
        """,
    )
    errs.extend(
        f"ticket set has no matching old_summary cell {tuple(r)!r}"
        for r in orphan
    )
    bad_form = _q(
        con,
        "SELECT DISTINCT t.shard_id FROM old_summary_ticket_set t "
        "JOIN catalog_shard c ON t.shard_id=c.shard_id "
        "WHERE c.form <> 'old_summary'"
    )
    errs.extend(
        f"ticket set attached to non-old_summary shard {r[0]!r}"
        for r in bad_form
    )
    mismatch = _q(
        con,
        """
        SELECT o.shard_id, o.old_customer, o.date_key, o.metric,
               o.distinct_ticket_cnt, COUNT(DISTINCT t.ticket)
        FROM old_summary_cell o
        JOIN old_summary_ticket_set t
          ON t.shard_id=o.shard_id AND t.old_customer=o.old_customer
         AND t.date_key=o.date_key AND t.metric=o.metric
        WHERE o.distinct_ticket_cnt IS NOT NULL
        GROUP BY 1,2,3,4
        HAVING o.distinct_ticket_cnt <> COUNT(DISTINCT t.ticket)
        """,
    )
    errs.extend(
        f"ticket set cardinality {r[5]} disagrees with distinct_ticket_cnt "
        f"{r[4]} on cell {(r[0], r[1], r[2], r[3])!r}"
        for r in mismatch
    )
    # Empty string is a valid ticket object; NULL is not.
    null_ticket = _q(
        con,
        "SELECT shard_id, old_customer FROM old_summary_ticket_set "
        "WHERE ticket IS NULL"
    )
    errs.extend(
        f"NULL ticket in set of shard {r[0]!r} customer {r[1]!r} "
        "(use '' for the empty-string object)"
        for r in null_ticket
    )
    return errs


def _check_metric_alignment(con: sqlite3.Connection) -> list[str]:
    errs: list[str] = []
    # old_summary cells must use the metric declared by their shard.
    rows = _q(
        con,
        "SELECT o.shard_id, o.metric, c.metric FROM old_summary_cell o "
        "JOIN catalog_shard c ON o.shard_id=c.shard_id "
        "WHERE o.metric <> c.metric"
    )
    errs.extend(
        f"old_summary cell metric {r[1]!r} differs from shard {r[0]!r} "
        f"catalog metric {r[2]!r}"
        for r in rows
    )
    # Detailed shards' metric comes only from the catalog.
    return errs


def run_all_checks(con: sqlite3.Connection) -> list[str]:
    """Execute every validation; return all problems found."""
    errs = _check_tables(con)
    if errs:
        return errs
    for fn in (
        _check_mapping,
        _check_catalog,
        _check_date_shapes,
        _check_atomic,
        _check_full_summary,
        _check_old_summary,
        _check_metric_alignment,
    ):
        errs.extend(fn(con))
    # Stable ordering for deterministic reports.
    return sorted(set(errs))


def mapping_versions(con: sqlite3.Connection) -> tuple[str, str]:
    """Return (old_version, new_version) in deterministic label order."""
    versions = sorted(r[0] for r in _q(con, "SELECT DISTINCT version FROM mapping"))
    if len(versions) != 2:
        raise ValueError("mapping must contain exactly two versions")
    return versions[0], versions[1]
