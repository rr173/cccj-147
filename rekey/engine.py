"""Re-key engine: catalog-driven rebuild on disk, never in Python memory.

Design contract
---------------
* The input database is attached read-only; the output is a brand new file.
* All grouping, min/sum/count and ticket-set unioning happens inside SQLite
  temporary/regular tables.  Ticket collections are NEVER fetched into
  Python as sets -- distinct counts are computed by SQL COUNT(DISTINCT).
* An old-summary cell is only movable when every participating member maps
  to the *same* new customer.  A split marks every target cell blocked for
  the aggregate part; no per-head split is ever invented.
* Exact ticket sets are merged like sets (cross-shard duplicates count
  once).  A bare distinct count is only carried when it is the *sole*
  distinct source for a cell; mixing it with exact sets (or with another
  count) blocks the distinct number because adding counts double-counts.
* On any validation failure nothing readable is left behind: the output
  file is written via a temp file and atomically renamed only on success.
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile

from . import checks
from .schema import OUTPUT_DDL


class CheckFailed(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors[:5]))
        self.errors = errors


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _attach_input(out: sqlite3.Connection, input_path: str) -> None:
    uri = "file:" + os.path.abspath(input_path) + "?mode=ro"
    out.execute("ATTACH DATABASE ? AS src", (uri,))


def _build_staging(out: sqlite3.Connection) -> None:
    """Map every recoverable contribution onto its NEW customer."""
    out.executescript(
        """
        -- Member -> customer lookup tables for both versions -----------
        CREATE TEMP TABLE map_old AS
        SELECT source, member_id, customer AS old_customer
        FROM src.mapping WHERE version =
            (SELECT MIN(version) FROM src.mapping);
        CREATE TEMP TABLE map_new AS
        SELECT source, member_id, customer AS new_customer
        FROM src.mapping WHERE version =
            (SELECT MAX(version) FROM src.mapping);
        CREATE UNIQUE INDEX ux_map_old ON map_old(source, member_id);
        CREATE UNIQUE INDEX ux_map_new ON map_new(source, member_id);

        -- Detailed contributions (atomic shards + full_summary shards)
        -- flattened into one stream, re-keyed through the NEW mapping.
        -- Coverage gaps are simply absent rows; nothing is imputed.
        CREATE TEMP TABLE detail_contrib AS
        SELECT c.metric,
               a.shard_id,
               n.new_customer,
               a.date_key,
               a.src_source AS source,
               a.seq,
               a.member_id,
               a.amount,
               a.ticket,
               a.physical_id
        FROM src.atomic_fact a
        JOIN src.catalog_shard c ON c.shard_id = a.shard_id
        JOIN map_new n ON n.source = a.src_source
                      AND n.member_id = a.member_id
        UNION ALL
        SELECT c.metric,
               s.shard_id,
               n.new_customer,
               s.date_key,
               s.member_source AS source,
               NULL AS seq,
               s.member_id,
               s.amount,
               s.ticket,
               s.physical_id
        FROM src.summary_contribution s
        JOIN src.catalog_shard c ON c.shard_id = s.shard_id
        JOIN map_new n ON n.source = s.member_source
                      AND n.member_id = s.member_id;
        CREATE INDEX ix_detail ON detail_contrib(
            new_customer, date_key, metric);

        -- Old-summary cells with their participating member set expanded.
        CREATE TEMP TABLE old_cell_member AS
        SELECT o.shard_id, o.old_customer, o.date_key, o.metric,
               o.sum_amount, o.cnt_nonnull, o.min_amount,
               o.distinct_ticket_cnt,
               je.value ->> '$[0]' AS source,
               je.value ->> '$[1]' AS member_id
        FROM src.old_summary_cell o,
             json_each(o.participating_members) je;

        -- How many new customers each old cell's members land on.
        -- target_count = 1 means the whole cell is movable.  (SQLite 3.40
        -- has no DISTINCT window aggregates, so compute via GROUP BY.)
        CREATE TEMP TABLE old_cell_target_count AS
        SELECT m.shard_id, m.old_customer, m.date_key, m.metric,
               COUNT(DISTINCT n.new_customer) AS target_count
        FROM old_cell_member m
        JOIN map_new n ON n.source = m.source AND n.member_id = m.member_id
        GROUP BY m.shard_id, m.old_customer, m.date_key, m.metric;

        CREATE TEMP TABLE old_cell_targets AS
        SELECT m.shard_id, m.old_customer, m.date_key, m.metric,
               m.sum_amount, m.cnt_nonnull, m.min_amount,
               m.distinct_ticket_cnt,
               n.new_customer,
               tc.target_count
        FROM old_cell_member m
        JOIN map_new n ON n.source = m.source AND n.member_id = m.member_id
        JOIN old_cell_target_count tc
          ON tc.shard_id = m.shard_id AND tc.old_customer = m.old_customer
         AND tc.date_key = m.date_key AND tc.metric = m.metric;

        CREATE TEMP TABLE old_cell_distinct AS
        SELECT DISTINCT shard_id, old_customer, date_key, metric,
               sum_amount, cnt_nonnull, min_amount, distinct_ticket_cnt,
               new_customer, target_count
        FROM old_cell_targets;
        """
    )


def _process_old_cells(out: sqlite3.Connection) -> None:
    """Lineage, carries and block decisions for old-summary shards."""
    out.executescript(
        """
        -- Lineage: movable cells ------------------------------------------------
        INSERT INTO cell_lineage (
            new_customer, date_key, metric, shard_id, lineage_kind,
            source, seq, member_id, old_customer, amount, ticket, note)
        SELECT new_customer, date_key, metric, shard_id, 'old_carry',
               NULL, NULL, NULL, old_customer, NULL, NULL,
               'all participating members re-key to this customer'
        FROM old_cell_distinct WHERE target_count = 1;

        -- Lineage: split cells, one blocked trail per target -------------------
        INSERT INTO cell_lineage (
            new_customer, date_key, metric, shard_id, lineage_kind,
            source, seq, member_id, old_customer, amount, ticket, note)
        SELECT new_customer, date_key, metric, shard_id, 'old_block',
               NULL, NULL, NULL, old_customer, NULL, NULL,
               'participating members split across new customers and no'
               || ' contribution-level detail exists'
        FROM old_cell_distinct WHERE target_count > 1;

        -- Unified aggregate stream ----------------------------------------------
        -- src_type:
        --   detail : atomic / full_summary row, individual amount
        --   carry  : whole movable old-summary cell, stored sum/cnt/min
        --   block  : split old-summary cell, poisons the aggregate cell
        CREATE TEMP TABLE src_agg AS
        SELECT metric, shard_id, new_customer, date_key,
               'detail' AS src_type, amount,
               0 AS cnt_value, CAST(NULL AS INTEGER) AS min_value, 0 AS blocked
        FROM detail_contrib
        UNION ALL
        SELECT d.metric, d.shard_id, d.new_customer, d.date_key,
               'carry', d.sum_amount, d.cnt_nonnull, d.min_amount, 0
        FROM old_cell_distinct d WHERE d.target_count = 1
        UNION ALL
        SELECT d.metric, d.shard_id, d.new_customer, d.date_key,
               'block', NULL, 0, NULL, 1
        FROM old_cell_distinct d WHERE d.target_count > 1;

        INSERT INTO blocked_reason (
            new_customer, date_key, metric, part, shard_id, reason)
        SELECT DISTINCT new_customer, date_key, metric, 'agg', shard_id,
               'old-summary cell for customer ' || old_customer
               || ' splits across new customers; only the aggregate'
               || ' sum/cnt/min and the participating member set were'
               || ' retained, so no contribution-level allocation is'
               || ' possible (no per-head splitting)'
        FROM old_cell_distinct WHERE target_count > 1;

        -- Unified distinct stream -----------------------------------------------
        -- exact: complete ticket object set available
        -- count: only distinct_ticket_cnt retained (a count, not detail)
        -- block: split cell, distinct number cannot be allocated at all
        CREATE TEMP TABLE src_dist AS
        SELECT d.metric, d.shard_id, d.new_customer, d.date_key,
               'exact' AS src_type, t.ticket,
               CAST(NULL AS INTEGER) AS count_value, 0 AS blocked
        FROM old_cell_distinct d
        JOIN src.old_summary_ticket_set t
          ON t.shard_id = d.shard_id AND t.old_customer = d.old_customer
         AND t.date_key = d.date_key AND t.metric = d.metric
        WHERE d.target_count = 1
        UNION ALL
        SELECT metric, shard_id, new_customer, date_key,
               'count', NULL, distinct_ticket_cnt, 0
        FROM old_cell_distinct
        WHERE target_count = 1 AND distinct_ticket_cnt IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM src.old_summary_ticket_set t
              WHERE t.shard_id = old_cell_distinct.shard_id
                AND t.old_customer = old_cell_distinct.old_customer
                AND t.date_key = old_cell_distinct.date_key
                AND t.metric = old_cell_distinct.metric)
        UNION ALL
        SELECT metric, shard_id, new_customer, date_key,
               'block', NULL, NULL, 1
        FROM old_cell_distinct WHERE target_count > 1;

        INSERT INTO blocked_reason (
            new_customer, date_key, metric, part, shard_id, reason)
        SELECT DISTINCT new_customer, date_key, metric, 'distinct', shard_id,
               'ticket distinct count of old customer ' || old_customer
               || ' cannot be allocated after a split without the complete'
               || ' ticket set'
        FROM old_cell_distinct WHERE target_count > 1;

        -- Detailed contribution tickets (atomic + full_summary) ---------------
        CREATE TEMP TABLE detail_ticket AS
        SELECT metric, shard_id, new_customer, date_key, ticket
        FROM detail_contrib
        WHERE ticket IS NOT NULL;
        CREATE INDEX ix_detail_ticket
            ON detail_ticket(new_customer, date_key, metric);
        """
    )


def _assemble_cells(out: sqlite3.Connection) -> None:
    out.executescript(
        """
        -- The universe of result keys -----------------------------------------
        CREATE TEMP TABLE cell_key AS
        SELECT DISTINCT new_customer, date_key, metric FROM src_agg
        UNION
        SELECT DISTINCT new_customer, date_key, metric FROM src_dist;

        -- Aggregate numbers.  All arithmetic stays in INTEGER; NULL amounts
        -- contribute to neither cnt nor min; an all-NULL cell yields
        -- sum NULL / cnt 0 / min NULL.
        CREATE TEMP TABLE agg_built AS
        SELECT k.new_customer, k.date_key, k.metric,
               SUM(CASE WHEN s.blocked = 0 AND s.src_type = 'detail'
                        THEN COALESCE(s.amount, 0) ELSE 0 END)
             + SUM(CASE WHEN s.blocked = 0 AND s.src_type = 'carry'
                        THEN COALESCE(s.amount, 0) ELSE 0 END)
                 AS sum_amount,
               SUM(CASE WHEN s.blocked = 0 AND s.src_type = 'detail'
                        AND s.amount IS NOT NULL THEN 1 ELSE 0 END)
             + SUM(CASE WHEN s.blocked = 0 AND s.src_type = 'carry'
                        THEN s.cnt_value ELSE 0 END)
                 AS cnt_nonnull,
               MIN(CASE WHEN s.blocked = 0 AND s.src_type = 'detail'
                        THEN s.amount END) AS min_detail,
               MIN(CASE WHEN s.blocked = 0 AND s.src_type = 'carry'
                        THEN s.min_value END) AS min_carry,
               MAX(s.blocked) AS blocked,
               MAX(CASE WHEN s.src_type = 'carry' THEN 1 ELSE 0 END)
                 AS any_carry,
               MAX(CASE WHEN s.src_type = 'detail' THEN 1 ELSE 0 END)
                 AS any_detail
        FROM cell_key k
        JOIN src_agg s ON s.new_customer = k.new_customer
                      AND s.date_key = k.date_key AND s.metric = k.metric
        GROUP BY k.new_customer, k.date_key, k.metric;

        -- Distinct source classification per cell.
        CREATE TEMP TABLE dist_class AS
        SELECT k.new_customer, k.date_key, k.metric,
               SUM(CASE WHEN s.src_type = 'count' THEN 1 ELSE 0 END)
                 AS n_count,
               SUM(CASE WHEN s.src_type = 'block' THEN 1 ELSE 0 END)
                 AS n_block,
               (SELECT COUNT(*) FROM detail_ticket t
                 WHERE t.new_customer = k.new_customer
                   AND t.date_key = k.date_key AND t.metric = k.metric)
                 AS n_detail_ticket,
               (SELECT COUNT(*) FROM src_dist z
                 WHERE z.new_customer = k.new_customer
                   AND z.date_key = k.date_key AND z.metric = k.metric
                   AND z.src_type = 'exact')
                 AS n_exact_rows
        FROM cell_key k
        LEFT JOIN src_dist s ON s.new_customer = k.new_customer
                            AND s.date_key = k.date_key
                            AND s.metric = k.metric
        GROUP BY k.new_customer, k.date_key, k.metric;

        -- Surviving exact ticket sets, merged IN SQLITE.  Cross-shard
        -- duplicates collapse through DISTINCT / the WITHOUT ROWID PK.
        INSERT INTO cell_ticket_set (new_customer, date_key, metric, ticket)
        SELECT DISTINCT new_customer, date_key, metric, ticket
        FROM src_dist WHERE src_type = 'exact' AND blocked = 0
        UNION
        SELECT DISTINCT new_customer, date_key, metric, ticket
        FROM detail_ticket;

        -- A bare count is carried ONLY when it is the sole distinct source.
        -- Two counts, count + exact set, or count + detail tickets block.
        INSERT INTO blocked_reason (
            new_customer, date_key, metric, part, shard_id, reason)
        SELECT DISTINCT d.new_customer, d.date_key, d.metric, 'distinct',
               s.shard_id,
               'only a distinct-ticket count (not the object set) was'
               || ' retained for this old-summary cell; distinct counts'
               || ' cannot be added to another count or to exact ticket'
               || ' sets, so the exact distinct number is unrecoverable'
        FROM dist_class d
        JOIN src_dist s ON s.new_customer = d.new_customer
                       AND s.date_key = d.date_key AND s.metric = d.metric
        WHERE s.src_type = 'count'
          AND (d.n_count > 1 OR d.n_exact_rows > 0
               OR d.n_detail_ticket > 0);

        CREATE TEMP TABLE distinct_built AS
        SELECT d.new_customer, d.date_key, d.metric,
               CASE
                   WHEN d.n_block > 0 THEN NULL
                   WHEN d.n_count = 1 AND d.n_exact_rows = 0
                        AND d.n_detail_ticket = 0
                        THEN (SELECT count_value FROM src_dist z
                              WHERE z.new_customer = d.new_customer
                                AND z.date_key = d.date_key
                                AND z.metric = d.metric
                                AND z.src_type = 'count' LIMIT 1)
                   WHEN (d.n_exact_rows + d.n_detail_ticket) > 0
                        THEN (SELECT COUNT(DISTINCT ticket)
                              FROM cell_ticket_set t
                              WHERE t.new_customer = d.new_customer
                                AND t.date_key = d.date_key
                                AND t.metric = d.metric)
                   ELSE NULL
               END AS distinct_ticket,
               CASE
                   WHEN d.n_block > 0 THEN 'blocked'
                   WHEN d.n_count = 1 AND d.n_exact_rows = 0
                        AND d.n_detail_ticket = 0 THEN 'carried'
                   WHEN d.n_count > 0 THEN 'blocked'
                   WHEN (d.n_exact_rows + d.n_detail_ticket) > 0
                        THEN 'rebuilt'
                   ELSE 'none'
               END AS distinct_status
        FROM dist_class d;

        -- Final result cells ----------------------------------------------------
        INSERT INTO fact_cell_new (
            new_customer, date_key, metric, sum_amount, cnt_nonnull,
            min_amount, distinct_ticket, agg_status, distinct_status)
        SELECT k.new_customer, k.date_key, k.metric,
               CASE WHEN a.blocked = 1 THEN NULL
                    WHEN a.cnt_nonnull = 0 THEN NULL
                    ELSE a.sum_amount END,
               CASE WHEN a.blocked = 1 THEN NULL ELSE a.cnt_nonnull END,
               CASE WHEN a.blocked = 1 THEN NULL ELSE
                   (SELECT MIN(v) FROM (
                        SELECT a.min_detail AS v
                        UNION ALL SELECT a.min_carry)
                    WHERE v IS NOT NULL) END,
               db.distinct_ticket,
               CASE WHEN a.blocked = 1 THEN 'blocked'
                    WHEN a.any_detail = 1 THEN 'rebuilt'
                    ELSE 'carried' END,
               COALESCE(db.distinct_status, 'none')
        FROM cell_key k
        JOIN agg_built a ON a.new_customer = k.new_customer
                        AND a.date_key = k.date_key
                        AND a.metric = k.metric
        LEFT JOIN distinct_built db ON db.new_customer = k.new_customer
                                   AND db.date_key = k.date_key
                                   AND db.metric = k.metric;
        """
    )


def _lineage_for_detail(out: sqlite3.Connection) -> None:
    out.execute(
        """
        INSERT INTO cell_lineage (
            new_customer, date_key, metric, shard_id, lineage_kind,
            source, seq, member_id, old_customer, amount, ticket, note)
        SELECT new_customer, date_key, metric, shard_id,
               CASE WHEN seq IS NULL THEN 'full_contrib' ELSE 'atomic' END,
               source, seq, member_id, NULL, amount, ticket,
               COALESCE(physical_id, '')
        FROM detail_contrib
        """
    )


def _metric_status(out: sqlite3.Connection) -> None:
    out.executescript(
        """
        INSERT INTO metric_shard_status
            (metric, shard_id, form, agg_status, distinct_status, detail)
        SELECT c.metric, c.shard_id, c.form,
               'unused', 'unused',
               'no fact row referenced this shard'
        FROM src.catalog_shard c
        WHERE c.shard_id NOT IN (SELECT DISTINCT shard_id FROM cell_lineage);

        INSERT INTO metric_shard_status
            (metric, shard_id, form, agg_status, distinct_status, detail)
        SELECT c.metric, c.shard_id, c.form,
               CASE WHEN EXISTS(SELECT 1 FROM blocked_reason b
                                WHERE b.shard_id = c.shard_id
                                  AND b.part = 'agg') THEN 'blocked'
                    WHEN c.form = 'old_summary' THEN 'carried'
                    ELSE 'rebuilt' END,
               CASE WHEN EXISTS(SELECT 1 FROM blocked_reason b
                                WHERE b.shard_id = c.shard_id
                                  AND b.part = 'distinct') THEN 'blocked'
                    WHEN c.form = 'old_summary' THEN 'carried'
                    ELSE 'rebuilt' END,
               'segment ' || c.src_source || ':[' || c.seq_start
               || ',' || c.seq_end || '] grain=' || c.date_grain
        FROM src.catalog_shard c
        WHERE c.shard_id IN (SELECT DISTINCT shard_id FROM cell_lineage);
        """
    )


def _final_guard(out: sqlite3.Connection) -> None:
    """Tail-of-batch re-check: any conflict now fails the whole run."""
    conflict = out.execute(
        """
        SELECT 1 FROM src.catalog_shard x
        JOIN src.catalog_shard y
          ON x.metric = y.metric AND x.src_source = y.src_source
         AND x.shard_id < y.shard_id
         AND x.seq_start <= y.seq_end
         AND y.seq_start <= x.seq_end LIMIT 1
        """
    ).fetchone()
    if conflict:
        raise CheckFailed(
            ["coverage conflict detected at batch tail; aborting"]
        )


def build(input_path: str, output_path: str, workdir: str | None = None,
          cache_mib: int = 8) -> dict:
    """Validate ``input_path`` and produce a complete re-key result database.

    Raises :class:`CheckFailed` (leaving no readable output file) on any
    input error.  The input database is opened read-only and never written.
    """
    ro = checks.open_input(input_path)
    try:
        errors = checks.run_all_checks(ro)
        versions = (
            checks.mapping_versions(ro) if not errors else (None, None)
        )
    finally:
        ro.close()
    if errors:
        raise CheckFailed(errors)

    workdir = workdir or os.path.dirname(os.path.abspath(output_path)) or "."
    os.makedirs(workdir, exist_ok=True)
    # Route SQLite's sort/hash temp files (and our own temp file) into the
    # chosen workdir so a large run spills on disk there rather than /tmp.
    prev_tmpdir = os.environ.get("TMPDIR")
    os.environ["TMPDIR"] = os.path.abspath(workdir)
    fd, tmp_path = tempfile.mkstemp(prefix=".rekey-", suffix=".db",
                                    dir=workdir)
    os.close(fd)
    os.unlink(tmp_path)  # sqlite creates the file itself
    try:
        out = sqlite3.connect(tmp_path)
        try:
            # Small page cache forces big sorts/sets to spill to temp files.
            out.execute(f"PRAGMA cache_size = -{max(1, cache_mib) * 1024}")
            # TEMP_STORE=1 in this build: sort/hash temp objects go to files
            # under TMPDIR, which we just pinned to the workdir.
            out.execute("PRAGMA temp_store = FILE")
            out.executescript(OUTPUT_DDL)
            _attach_input(out, input_path)
            # No explicit transaction: executescript() commits prior work,
            # and the temp-file + os.replace below is the atomicity
            # guarantee -- a failure leaves only an unlinked temp file.
            _build_staging(out)
            _process_old_cells(out)
            _assemble_cells(out)
            _lineage_for_detail(out)
            _metric_status(out)
            _final_guard(out)
            out.execute(
                "INSERT INTO catalog_snapshot "
                "SELECT shard_id, form, date_grain, metric, src_source, "
                "seq_start, seq_end FROM src.catalog_shard"
            )
            old_v, new_v = versions
            meta = {
                "input_path": os.path.abspath(input_path),
                "input_sha256": _sha256_file(input_path),
                "mapping_old_version": old_v,
                "mapping_new_version": new_v,
                "status": "complete",
            }
            out.executemany(
                "INSERT INTO run_meta(key,value) VALUES(?,?)",
                list(meta.items()),
            )
            out.commit()
            out.execute("DETACH DATABASE src")
        except Exception:
            out.close()
            raise
        else:
            out.close()
        os.replace(tmp_path, output_path)
        return meta
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
    finally:
        if prev_tmpdir is None:
            os.environ.pop("TMPDIR", None)
        else:
            os.environ["TMPDIR"] = prev_tmpdir
