"""SQLite table contracts for the fact re-key tool.

Inputs are supplied in ONE read-only SQLite database with the tables whose
DDL is in :data:`INPUT_DDL`.  The tool never writes to that database.

Member identity everywhere is the composite ``(source, member_id)``.
A mapping version gives each member exactly one customer; the two versions
share the same member universe and differ only in the customer labels.

Facts come in three shard *forms* declared in ``catalog_shard``:

* ``atomic``       : individual contributions in ``atomic_fact`` with
                     source/seq inside the shard's closed coverage segment;
* ``full_summary`` : member-level aggregate lines in
                     ``summary_contribution`` that still preserve every
                     contribution's amount/ticket and the participating set;
* ``old_summary``  : only old customer / date / metric / value plus the
                     participating member set, in ``old_summary_cell``.

Exact business-object sets (for exact-deduplication metrics) are optional
rows in ``old_summary_ticket_set``.  Without a set an old-summary cell has
only ``distinct_ticket_cnt`` -- a count, never preserved detail.
"""

INPUT_DDL = """
PRAGMA journal_mode = WAL;

CREATE TABLE mapping (
    version    TEXT NOT NULL,           -- e.g. 'old' / 'new'
    source     TEXT NOT NULL,
    member_id  TEXT NOT NULL,
    customer   TEXT NOT NULL,
    PRIMARY KEY (version, source, member_id)
);

CREATE TABLE catalog_shard (
    shard_id   TEXT PRIMARY KEY,        -- shards are fact partitions
    form       TEXT NOT NULL
               CHECK (form IN ('atomic','full_summary','old_summary')),
    date_grain TEXT NOT NULL,           -- 'day' | 'month' | 'year'
    metric     TEXT NOT NULL,
    src_source TEXT NOT NULL,
    seq_start  INTEGER NOT NULL,
    seq_end    INTEGER NOT NULL,
    CHECK (seq_end >= seq_start)
    -- one segment never crosses sources: the segment belongs to src_source.
);

-- Form atomic: every contribution, fully identifiable.
CREATE TABLE atomic_fact (
    shard_id    TEXT NOT NULL,
    src_source  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    member_id   TEXT NOT NULL,
    date_key    TEXT NOT NULL,
    amount      INTEGER,                -- nullable; never a float
    ticket      TEXT,                   -- nullable; '' is a valid object
    physical_id TEXT,                   -- nullable physical contribution id
    PRIMARY KEY (shard_id, src_source, seq)
);

-- Form full_summary: per (member,date) aggregation but every contribution's
-- values are preserved (amount/ticket rows + participating member set).
CREATE TABLE summary_contribution (
    shard_id    TEXT NOT NULL,
    member_source TEXT NOT NULL,
    member_id   TEXT NOT NULL,
    date_key    TEXT NOT NULL,
    amount      INTEGER,
    ticket      TEXT,
    physical_id TEXT,
    row_idx     INTEGER NOT NULL        -- disambiguates same-value rows
);

-- Form old_summary: no amount detail beyond one set of aggregate values.
CREATE TABLE old_summary_cell (
    shard_id          TEXT NOT NULL,
    old_customer      TEXT NOT NULL,
    date_key          TEXT NOT NULL,
    metric            TEXT NOT NULL,
    sum_amount        INTEGER,          -- null when every amount was null
    cnt_nonnull       INTEGER NOT NULL, -- non-null amount count
    min_amount        INTEGER,          -- null iff cnt_nonnull = 0
    distinct_ticket_cnt INTEGER,         -- null = count unknown/not provided
    participating_members TEXT NOT NULL, -- JSON [["source","member_id"],...]
    PRIMARY KEY (shard_id, old_customer, date_key, metric)
);

-- Optional complete business-object sets for old_summary cells.
-- Every row is one distinct ticket object; the set is exact.
CREATE TABLE old_summary_ticket_set (
    shard_id     TEXT NOT NULL,
    old_customer TEXT NOT NULL,
    date_key     TEXT NOT NULL,
    metric       TEXT NOT NULL,
    ticket       TEXT NOT NULL,         -- '' is a valid object
    PRIMARY KEY (shard_id, old_customer, date_key, metric, ticket)
);
"""

OUTPUT_DDL = """
CREATE TABLE run_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- New customer-grain result.  One row per (new customer, date, metric).
CREATE TABLE fact_cell_new (
    new_customer   TEXT NOT NULL,
    date_key       TEXT NOT NULL,
    metric         TEXT NOT NULL,
    sum_amount     INTEGER,             -- null: all-null sums or blocked
    cnt_nonnull    INTEGER,             -- null when blocked
    min_amount     INTEGER,
    distinct_ticket INTEGER,            -- null when blocked/unrecoverable
    agg_status     TEXT NOT NULL,       -- rebuilt|carried|blocked
    distinct_status TEXT NOT NULL,      -- rebuilt|carried|blocked|none
    PRIMARY KEY (new_customer, date_key, metric)
);

-- Why a cell is blocked, one row per missing piece of information.
CREATE TABLE blocked_reason (
    new_customer TEXT NOT NULL,
    date_key     TEXT NOT NULL,
    metric       TEXT NOT NULL,
    part         TEXT NOT NULL,         -- agg | distinct
    shard_id     TEXT NOT NULL,
    reason       TEXT NOT NULL
);

-- Per metric x shard recovery status (the provenance declaration).
CREATE TABLE metric_shard_status (
    metric        TEXT NOT NULL,
    shard_id      TEXT NOT NULL,
    form          TEXT NOT NULL,
    agg_status    TEXT NOT NULL,        -- rebuilt|carried|blocked|unused
    distinct_status TEXT NOT NULL,      -- rebuilt|carried|blocked|unused
    detail        TEXT NOT NULL,
    PRIMARY KEY (metric, shard_id)
);

-- Every contribution / carried cell feeding a result cell (the explain trail).
CREATE TABLE cell_lineage (
    new_customer TEXT NOT NULL,
    date_key     TEXT NOT NULL,
    metric       TEXT NOT NULL,
    shard_id     TEXT NOT NULL,
    lineage_kind TEXT NOT NULL,  -- atomic|full_contrib|old_carry|old_block
    source       TEXT,
    seq          INTEGER,
    member_id    TEXT,
    old_customer TEXT,
    amount       INTEGER,
    ticket       TEXT,
    note         TEXT
);

-- Exact surviving ticket sets, kept IN SQLITE (never materialised in Python).
CREATE TABLE cell_ticket_set (
    new_customer TEXT NOT NULL,
    date_key     TEXT NOT NULL,
    metric       TEXT NOT NULL,
    ticket       TEXT NOT NULL,
    PRIMARY KEY (new_customer, date_key, metric, ticket)
) WITHOUT ROWID;

-- Verbatim catalog snapshot so a new process can interpret the result.
CREATE TABLE catalog_snapshot (
    shard_id   TEXT PRIMARY KEY,
    form       TEXT NOT NULL,
    date_grain TEXT NOT NULL,
    metric     TEXT NOT NULL,
    src_source TEXT NOT NULL,
    seq_start  INTEGER NOT NULL,
    seq_end    INTEGER NOT NULL
);
"""

# Accepted date granularities and the exact key shape each one must have.
DATE_GRAIN = {
    "day":   (r"^\d{4}-\d{2}-\d{2}$", "YYYY-MM-DD"),
    "month": (r"^\d{4}-\d{2}$", "YYYY-MM"),
    "year":  (r"^\d{4}$", "YYYY"),
}
