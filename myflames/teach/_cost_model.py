"""Cost-model primitives for the `teach` lessons.

Every constant and formula here must correspond to a documented MySQL 8.4
or MariaDB 11.x behaviour. The teach HTML pages render numbers computed
from these functions on the fly (sliders), and the Python tests in
``test/test_teach.py`` assert exact values for known inputs so a version
upgrade that changes defaults is caught loudly.

Sources:

* InnoDB B+tree / page structure:
  MySQL 8.4 reference manual §17.6.2 "InnoDB Indexes" and
  §17.11.2 "File Space Management".
* ``join_buffer_size`` default:
  MySQL 8.4 reference manual §7.1.8 "Server System Variables".
  MariaDB 11.4 Knowledge Base "Server System Variables".
* Hash join & BNL removal in MySQL 8.0.20:
  "What's New in MySQL 8.0.20" release notes.
* MariaDB block-based join algorithms and ``join_cache_level``:
  MariaDB Knowledge Base "Block-based Join Algorithms".
* InnoDB buffer-pool midpoint-insertion LRU:
  MySQL 8.4 reference manual §17.5.1 "Buffer Pool" and §17.8.3.3
  "Making the Buffer Pool Scan Resistant".
"""
from __future__ import annotations

import math
from typing import Dict, List, NamedTuple

# ---------------------------------------------------------------------------
# Version-default constants (asserted by tests so upgrades break loudly)
# ---------------------------------------------------------------------------

#: InnoDB page size default (bytes). Configurable at build time (4/8/16/32/64 KiB)
#: but every default deployment uses 16 KiB.
INNODB_PAGE_SIZE_DEFAULT = 16 * 1024  # 16 KiB

#: Per-page overhead we deduct from ``page_size`` before computing fan-out:
#: 38 B file-header + 56 B page-header + 8 B page-trailer + ~18 B infimum/supremum
#: records + page directory slots. Conservative round number.
INNODB_PAGE_OVERHEAD_BYTES = 120

#: Secondary-index non-leaf child-pointer size (page number, 4 B) + record header
#: (~5 B). Used by ``innodb_fanout``.
INNODB_CHILD_POINTER_BYTES = 9

#: ``join_buffer_size`` default in both MySQL 8.4 and MariaDB 11.4 LTS.
JOIN_BUFFER_SIZE_DEFAULT = 256 * 1024  # 256 KiB = 262144 B

#: MariaDB ``join_cache_level`` default. 2 = BNL without hashing.
MARIADB_JOIN_CACHE_LEVEL_DEFAULT = 2

#: MySQL version in which BNL was removed in favour of hash join for
#: non-indexed equi-joins. Used in the ``bnl`` lesson's warning banner.
MYSQL_BNL_REMOVED_IN = "8.0.20"

#: InnoDB buffer-pool midpoint-insertion LRU knobs.
INNODB_OLD_BLOCKS_PCT_DEFAULT = 37
INNODB_OLD_BLOCKS_TIME_DEFAULT_MS = 1000

# ---------------------------------------------------------------------------
# InnoDB B+tree
# ---------------------------------------------------------------------------


def innodb_fanout(key_size: int, page_size: int = INNODB_PAGE_SIZE_DEFAULT) -> int:
    """Return the approximate non-leaf fan-out for an InnoDB B+tree page.

    Non-leaf pages store ``(key, child_page_no)`` records. The effective
    entries per page is roughly::

        (page_size - overhead) // (key_size + child_pointer_overhead)

    This is a first-order estimate; real InnoDB pages have variable-length
    record headers and a 15/16 fill factor for splits, so real-world fan-out
    is a bit lower. The lesson UI carries the caveat explicitly.
    """
    if key_size <= 0 or page_size <= 0:
        raise ValueError("key_size and page_size must be positive")
    usable = page_size - INNODB_PAGE_OVERHEAD_BYTES
    entry = key_size + INNODB_CHILD_POINTER_BYTES
    if entry <= 0:
        raise ValueError("entry size computed as <= 0")
    return max(2, usable // entry)


def innodb_tree_height(rows: int, fan_out: int) -> int:
    """Return B+tree height (number of page levels traversed, including leaf).

    Uses ``ceil(log_fanout(rows))`` with a floor of 2. The "every level has
    roughly ``fan_out`` children" assumption is a simplification that ignores
    clustered-leaf density (where a leaf holds a *row*, not a key). It matches
    real-world published InnoDB heights:

    * 1M rows with BIGINT PK → 3 levels
    * 1B rows with BIGINT PK → 4 levels

    (See Jeremy Cole's innodb_ruby measurements.)

    Smallest meaningful value is 2 — a tree with ``rows <= fan_out`` still
    counts as "root + one level of leaves".
    """
    if rows <= 0:
        return 2
    if fan_out <= 1:
        raise ValueError("fan_out must be > 1")
    if rows <= fan_out:
        return 2
    return max(2, int(math.ceil(math.log(rows, fan_out))))


class BTreeLookupCost(NamedTuple):
    fan_out: int
    height: int
    pages_touched: int
    traversals: int
    explanation: str


def btree_lookup_cost(
    rows: int,
    key_size: int = 8,
    page_size: int = INNODB_PAGE_SIZE_DEFAULT,
    key_type: str = "pk",  # "pk" | "secondary_covering" | "secondary_noncovering"
) -> BTreeLookupCost:
    """Estimate the pages touched for a single-row lookup.

    * ``pk``: one traversal of the clustered B+tree (leaves contain full rows).
    * ``secondary_covering``: one traversal of the secondary index (leaves contain
      the needed columns + PK; no clustered-tree walk needed).
    * ``secondary_noncovering``: two traversals — secondary index, then clustered
      index to fetch the row proper.
    """
    fan_out = innodb_fanout(key_size, page_size)
    height = innodb_tree_height(rows, fan_out)
    if key_type == "pk":
        traversals = 1
        explanation = (
            f"Clustered PK lookup: walk the clustered B+tree "
            f"({height} page levels). The leaf page holds the full row — "
            f"no extra I/O."
        )
    elif key_type == "secondary_covering":
        traversals = 1
        explanation = (
            f"Covering secondary index: walk the secondary B+tree "
            f"({height} levels). The leaf page already holds every column "
            f"the query asked for — no clustered-tree visit."
        )
    elif key_type == "secondary_noncovering":
        traversals = 2
        explanation = (
            f"Non-covering secondary lookup: walk the secondary B+tree "
            f"({height} levels), then walk the clustered B+tree ({height} "
            f"levels) once per matching row to fetch the actual row. "
            f"This is why covering indexes win."
        )
    else:
        raise ValueError(f"unknown key_type: {key_type!r}")
    return BTreeLookupCost(
        fan_out=fan_out,
        height=height,
        pages_touched=height * traversals,
        traversals=traversals,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# InnoDB B+tree shape (pages per level), used by the 3D B+tree view
# ---------------------------------------------------------------------------

#: Extra bytes in front of every COMPACT/DYNAMIC record
#: (``REC_N_NEW_EXTRA_BYTES``, storage/innobase/rem/rec.h). The 9 B
#: ``INNODB_CHILD_POINTER_BYTES`` above is this header plus the 4 B child page
#: number (``REC_NODE_PTR_SIZE``).
INNODB_RECORD_HEADER_BYTES = 5

#: Hidden columns in every clustered-index leaf record: ``DB_TRX_ID`` (6 B) and
#: ``DB_ROLL_PTR`` (7 B) (``DATA_TRX_ID_LEN``, ``DATA_ROLL_PTR_LEN``,
#: storage/innobase/include/data0type.h).
INNODB_CLUSTERED_SYSTEM_BYTES = 13

#: ``BIGINT`` primary key size used by the 3D view's ``users.id``.
PK_BIGINT_BYTES = 8

#: Clustered-index leaf pages filled by in-order inserts keep 1/16 of the
#: page free for future updates: ``btr_cur_optimistic_insert`` splits instead
#: once less than ``dict_index_get_space_reserve()`` (page size / 16) would be
#: left (storage/innobase/btr/btr0cur.cc, include/dict0dict.ic; same in
#: MariaDB). Secondary indexes and non-leaf pages have no such reserve.
INNODB_CLUSTERED_LEAF_RESERVE_DIVISOR = 16


def innodb_leaf_capacity(entry_bytes: int, page_size: int = INNODB_PAGE_SIZE_DEFAULT,
                         reserve_bytes: int = 0) -> int:
    """Return how many leaf records of *entry_bytes* fit in one page.

    Same first-order estimate as ``innodb_fanout``: the
    ``INNODB_PAGE_OVERHEAD_BYTES`` round number for the page headers, minus
    *reserve_bytes* kept free (the clustered leaf reserve). The page
    directory (2 bytes per 4 to 8 records) is left out. Pages that take
    random inserts split in the middle and are often much emptier, so a real
    tree can have more leaf pages.
    """
    if entry_bytes <= 0 or page_size <= 0:
        raise ValueError("entry_bytes and page_size must be positive")
    if reserve_bytes < 0:
        raise ValueError("reserve_bytes must not be negative")
    return max(1, (page_size - INNODB_PAGE_OVERHEAD_BYTES - reserve_bytes) // entry_bytes)


def innodb_btree_shape(rows: int, leaf_entry_bytes: int, node_key_bytes: int,
                       page_size: int = INNODB_PAGE_SIZE_DEFAULT,
                       leaf_reserve_bytes: int = 0) -> List[int]:
    """Return the page count of every level, root first, leaf last.

    Leaves hold ``innodb_leaf_capacity(leaf_entry_bytes, page_size,
    leaf_reserve_bytes)`` records; each level above holds
    ``innodb_fanout(node_key_bytes)`` child pointers per page, with every
    page full except the last. A table whose rows fit in one page is a single
    page that is both root and leaf, as in InnoDB (``btr_create`` makes the
    root at level 0).
    """
    if rows < 0:
        raise ValueError("rows must not be negative")
    capacity = innodb_leaf_capacity(leaf_entry_bytes, page_size, leaf_reserve_bytes)
    fan_out = innodb_fanout(node_key_bytes, page_size)
    levels = [max(1, -(-rows // capacity))]
    while levels[-1] > 1:
        levels.append(-(-levels[-1] // fan_out))
    levels.reverse()
    return levels


def innodb_index_shapes(rows: int, row_bytes: int, email_bytes: int,
                        page_size: int = INNODB_PAGE_SIZE_DEFAULT) -> Dict[str, Dict[str, object]]:
    """Shape of ``users`` (clustered on ``id BIGINT``) and ``idx_users_email``.

    * Clustered leaf record: column data (*row_bytes*, the ``id`` included) +
      the fixed record header + ``DB_TRX_ID`` + ``DB_ROLL_PTR``. Leaves keep
      the 1/16 update reserve. Node pointers hold the primary key.
    * Secondary leaf record: email + primary key + the fixed record header.
      Node pointers of a secondary index hold every index field, the primary
      key included (``dict_index_get_n_unique_in_tree``), so its fan-out
      depends on email + id. Its pages are counted full.

    The fixed header is 5 bytes; DYNAMIC records also store null flags and a
    length byte or two per variable-length column, which *row_bytes* and
    *email_bytes* are meant to include.
    """
    clustered_leaf = row_bytes + INNODB_RECORD_HEADER_BYTES + INNODB_CLUSTERED_SYSTEM_BYTES
    secondary_leaf = email_bytes + PK_BIGINT_BYTES + INNODB_RECORD_HEADER_BYTES
    reserve = page_size // INNODB_CLUSTERED_LEAF_RESERVE_DIVISOR
    shapes = {}
    for name, leaf_bytes, node_key, leaf_reserve in (
            ("clustered", clustered_leaf, PK_BIGINT_BYTES, reserve),
            ("secondary", secondary_leaf, email_bytes + PK_BIGINT_BYTES, 0)):
        levels = innodb_btree_shape(rows, leaf_bytes, node_key, page_size, leaf_reserve)
        shapes[name] = {
            "levels": levels,
            "fan_out": innodb_fanout(node_key, page_size),
            "leaf_capacity": innodb_leaf_capacity(leaf_bytes, page_size, leaf_reserve),
            "leaf_entry_bytes": leaf_bytes,
            "leaf_reserve_bytes": leaf_reserve,
            "pages": sum(levels),
        }
    return shapes


def leaf_range_pages(first_id: int, last_id: int, leaf_capacity: int) -> int:
    """Leaf pages a primary key range ``first_id..last_id`` spans.

    Assumes ids 1..N packed in order, leaf *i* holding ids
    ``i * capacity + 1`` to ``(i + 1) * capacity``. A range scan reads the
    first of them by descending the tree and each later one through the
    leaf's next-page link.
    """
    if first_id < 1 or last_id < first_id or leaf_capacity < 1:
        raise ValueError("need 1 <= first_id <= last_id and leaf_capacity >= 1")
    return (last_id - 1) // leaf_capacity - (first_id - 1) // leaf_capacity + 1


# ---------------------------------------------------------------------------
# Block Nested Loop join (MariaDB) — deprecated in MySQL 8.4
# ---------------------------------------------------------------------------


def bnl_blocks(
    outer_rows: int,
    row_size: int,
    join_buffer_size: int = JOIN_BUFFER_SIZE_DEFAULT,
) -> int:
    """How many blocks the outer table fills in the join buffer.

    The inner table is re-scanned once per block. This is the entire reason
    ``join_buffer_size`` matters for BNL performance.
    """
    if outer_rows <= 0 or row_size <= 0 or join_buffer_size <= 0:
        return 1
    rows_per_block = max(1, join_buffer_size // row_size)
    return max(1, math.ceil(outer_rows / rows_per_block))


class BNLCost(NamedTuple):
    blocks: int
    inner_scans: int
    row_comparisons: int
    rows_per_block: int
    explanation: str


def bnl_cost(
    outer_rows: int,
    inner_rows: int,
    row_size: int,
    join_buffer_size: int = JOIN_BUFFER_SIZE_DEFAULT,
) -> BNLCost:
    """Return a cost breakdown for a MariaDB-style Block Nested Loop join."""
    blocks = bnl_blocks(outer_rows, row_size, join_buffer_size)
    rows_per_block = max(1, join_buffer_size // row_size)
    inner_scans = blocks
    row_comparisons = blocks * inner_rows * min(rows_per_block, outer_rows)
    explanation = (
        f"Outer rows are packed into {blocks} block(s) of up to {rows_per_block} "
        f"rows each. The inner table is fully re-scanned once per block "
        f"({inner_scans} scan(s)). Bigger join_buffer_size → fewer blocks → "
        f"fewer inner re-scans."
    )
    return BNLCost(
        blocks=blocks,
        inner_scans=inner_scans,
        row_comparisons=row_comparisons,
        rows_per_block=rows_per_block,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Hash join (MySQL 8.4 & MariaDB ``join_cache_level=4``)
# ---------------------------------------------------------------------------


def hash_join_memory(build_rows: int, row_size: int) -> int:
    """Approximate in-memory hash-table size (bytes) for the build side.

    Adds ~40% bucket/chain overhead over raw row bytes.
    """
    if build_rows <= 0 or row_size <= 0:
        return 0
    return int(build_rows * row_size * 1.4)


class HashJoinCost(NamedTuple):
    build_bytes: int
    fits_in_memory: bool
    spilled: bool
    partitions: int
    phases: int
    explanation: str


def hash_join_cost(
    build_rows: int,
    probe_rows: int,
    row_size: int,
    join_buffer_size: int = JOIN_BUFFER_SIZE_DEFAULT,
) -> HashJoinCost:
    """Return a cost breakdown for MySQL 8.4-style hash join.

    When the in-memory build exceeds ``join_buffer_size`` MySQL switches to
    grace-hash partitioning: both inputs are partitioned to disk, and each
    partition is probed independently. The total I/O doubles (write + read
    both inputs once each).
    """
    build_bytes = hash_join_memory(build_rows, row_size)
    fits = build_bytes <= join_buffer_size
    spilled = not fits
    if spilled and build_bytes > 0:
        # Number of partitions needed so each partition's build side fits.
        partitions = max(2, math.ceil(build_bytes / join_buffer_size))
    else:
        partitions = 1
    phases = 2 if not spilled else 4  # build + probe (+ partition write + partition re-read)
    if spilled:
        explanation = (
            f"Build side is {build_bytes:,} bytes — bigger than "
            f"join_buffer_size ({join_buffer_size:,} bytes). MySQL spills: "
            f"partition both inputs into {partitions} chunks on disk, then "
            f"probe each partition separately. Cost doubles (write + read)."
        )
    else:
        explanation = (
            f"Build side is {build_bytes:,} bytes — fits in join_buffer_size "
            f"({join_buffer_size:,} bytes). Single in-memory hash table; "
            f"probe streams through in one pass. O(build + probe)."
        )
    return HashJoinCost(
        build_bytes=build_bytes,
        fits_in_memory=fits,
        spilled=spilled,
        partitions=partitions,
        phases=phases,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# InnoDB buffer-pool midpoint-insertion LRU
# ---------------------------------------------------------------------------


class LRUState(NamedTuple):
    pool_size: int
    young_capacity: int
    old_capacity: int
    evictions: int
    promotions: int
    young_pages: int  # current population
    old_pages: int  # current population
    explanation: str


def simulate_midpoint_lru(
    pool_size: int,
    access_trace: list,
    old_blocks_pct: int = INNODB_OLD_BLOCKS_PCT_DEFAULT,
    old_blocks_time_ms: int = INNODB_OLD_BLOCKS_TIME_DEFAULT_MS,
) -> LRUState:
    """Simulate InnoDB's midpoint-insertion LRU against an access trace.

    ``access_trace`` is a list of ``(page_id, timestamp_ms)`` tuples. Returns
    a summary of evictions and young/old populations. Used only by tests —
    the in-page JS re-implements the same logic client-side.

    Algorithm (matches MySQL 8.4 reference manual §17.5.1):

    * On miss: the new page is inserted at the **midpoint** of the list
      (i.e. at the head of the old sublist), pushing the tail of old out.
    * On hit in the old sublist: if ``now - first_access >= old_blocks_time``,
      promote the page to the head of the young sublist. Otherwise, leave it
      where it is (this is what kills full-scan pollution).
    * On hit in the young sublist: move to the head of young (cheap list op).
    """
    if pool_size < 2:
        raise ValueError("pool_size must be >= 2")
    if not (0 <= old_blocks_pct <= 100):
        raise ValueError("old_blocks_pct must be in [0, 100]")

    young_capacity = max(1, pool_size * (100 - old_blocks_pct) // 100)
    old_capacity = pool_size - young_capacity

    # Each entry: {"id": int, "sublist": "young"|"old", "first_seen": int}
    young: list = []  # head is the MRU end
    old: list = []
    evictions = 0
    promotions = 0

    def find(pid):
        for lst, name in ((young, "young"), (old, "old")):
            for idx, entry in enumerate(lst):
                if entry["id"] == pid:
                    return lst, name, idx, entry
        return None

    for pid, now_ms in access_trace:
        found = find(pid)
        if found is None:
            # Miss → insert at midpoint (head of old). Evict tail of old first.
            if len(old) >= old_capacity:
                old.pop()  # tail = LRU end
                evictions += 1
            old.insert(0, {"id": pid, "sublist": "old", "first_seen": now_ms})
        else:
            lst, name, idx, entry = found
            if name == "old":
                age = now_ms - entry["first_seen"]
                if age >= old_blocks_time_ms:
                    # Promote to head of young
                    lst.pop(idx)
                    if len(young) >= young_capacity:
                        demoted = young.pop()  # tail of young
                        demoted["sublist"] = "old"
                        demoted["first_seen"] = now_ms
                        old.insert(0, demoted)
                    young.insert(0, {"id": pid, "sublist": "young", "first_seen": now_ms})
                    promotions += 1
                # else: stay in old (scan-resistance — the whole point)
            else:
                # Young hit: MRU bump to head of young. No promotion counter
                # because it was already young.
                lst.pop(idx)
                young.insert(0, entry)

    explanation = (
        f"InnoDB split the {pool_size}-page pool into young ({young_capacity}) "
        f"and old ({old_capacity}) sublists. After the trace: {len(young)} "
        f"young, {len(old)} old, {evictions} evictions, {promotions} promotions "
        f"from old → young. Textbook LRU would have different numbers — see "
        f"the comparison panel."
    )
    return LRUState(
        pool_size=pool_size,
        young_capacity=young_capacity,
        old_capacity=old_capacity,
        evictions=evictions,
        promotions=promotions,
        young_pages=len(young),
        old_pages=len(old),
        explanation=explanation,
    )


def simulate_classic_lru(pool_size: int, access_trace: list) -> Dict[str, int]:
    """Simulate a textbook single-list LRU for comparison."""
    if pool_size < 1:
        raise ValueError("pool_size must be >= 1")
    lst: list = []
    evictions = 0
    hits = 0
    for pid, _now_ms in access_trace:
        if pid in lst:
            lst.remove(pid)
            lst.insert(0, pid)
            hits += 1
        else:
            if len(lst) >= pool_size:
                lst.pop()
                evictions += 1
            lst.insert(0, pid)
    return {"evictions": evictions, "hits": hits, "final_population": len(lst)}


# ---------------------------------------------------------------------------
# Filesort (MySQL / MariaDB)
# ---------------------------------------------------------------------------

#: ``sort_buffer_size`` default in MySQL 8.4 (bytes). MariaDB 11.4 default
#: is also 256 KiB (262144).
SORT_BUFFER_SIZE_DEFAULT = 256 * 1024  # 256 KiB = 262144 B


class FilesortCost(NamedTuple):
    rows_per_run: int
    sorted_runs: int
    merge_passes: int
    total_io_rows: int
    explanation: str


def filesort_cost(
    rows: int,
    row_size: int,
    sort_buffer_size: int = SORT_BUFFER_SIZE_DEFAULT,
) -> FilesortCost:
    """Return a cost breakdown for MySQL's filesort algorithm.

    When the rows to sort fit in ``sort_buffer_size``, the sort is pure
    in-memory (quicksort). When they don't, MySQL fills the buffer,
    sorts it, writes a sorted run to tmpdir, repeats, then does a
    k-way merge of all runs.

    ``merge_passes`` approximates the number of merge-sort passes needed
    if MySQL can merge at most ~15 runs per pass (the merge-file fan-in
    documented in the MySQL source ``sql/filesort.cc``).
    """
    if rows <= 0 or row_size <= 0 or sort_buffer_size <= 0:
        return FilesortCost(
            rows_per_run=0, sorted_runs=1, merge_passes=0,
            total_io_rows=0, explanation="No rows to sort.",
        )
    rows_per_run = max(1, sort_buffer_size // row_size)
    sorted_runs = max(1, math.ceil(rows / rows_per_run))
    if sorted_runs <= 1:
        return FilesortCost(
            rows_per_run=rows_per_run, sorted_runs=1, merge_passes=0,
            total_io_rows=rows,
            explanation=(
                f"All {rows:,} rows fit in sort_buffer_size "
                f"({sort_buffer_size:,} bytes) — pure in-memory quicksort, "
                f"no disk spill."
            ),
        )
    # Each merge pass can combine up to MERGE_FAN_IN runs.
    merge_fan_in = 15
    merge_passes = max(1, math.ceil(math.log(sorted_runs) / math.log(merge_fan_in)))
    # I/O: each merge pass reads + writes all rows once.
    total_io_rows = rows + merge_passes * rows * 2
    explanation = (
        f"sort_buffer_size holds {rows_per_run:,} rows → {sorted_runs:,} sorted "
        f"run(s) spilled to tmpdir. The k-way merge needs {merge_passes} pass(es) "
        f"(fan-in ~{merge_fan_in}). Each pass reads + writes all {rows:,} rows. "
        f"Bigger sort_buffer_size → fewer runs → fewer merge passes."
    )
    return FilesortCost(
        rows_per_run=rows_per_run,
        sorted_runs=sorted_runs,
        merge_passes=merge_passes,
        total_io_rows=total_io_rows,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Internal temporary tables (in-memory → on-disk conversion)
# ---------------------------------------------------------------------------
#
# The in-memory limit depends on which engine holds the table. Verified
# against the MySQL tag mysql-8.4.9 and the MariaDB tag mariadb-11.4.9:
#
# * MySQL picks the in-memory engine from internal_tmp_mem_storage_engine,
#   default TempTable (sql/sys_vars.cc:5121, DEFAULT(TMP_TABLE_TEMPTABLE)).
# * The row cap takes min(tmp_table_size, max_heap_table_size) only for a
#   MEMORY (heap_hton) table; every other engine gets tmp_table_size alone
#   (sql/sql_tmp_table.cc:2226).
# * TempTable hands tmp_table_size to each table as its per-table limit
#   (storage/temptable/src/handler.cc:185) and throws RECORD_FILE_FULL once
#   that table's allocations would pass it
#   (storage/temptable/include/temptable/allocator.h:627-629). This
#   per-table limit exists since MySQL 8.0.28 (absent from mysql-8.0.27).
# * Every TempTable table also draws from one global RAM pool,
#   temptable_max_ram (default: 3% of physical memory clamped to 1-4 GiB,
#   sql/sys_vars.cc:5130). Past it, TempTable uses mmap files only within
#   temptable_max_mmap (default 0, sql/sys_vars.cc:5141; in 8.4 also only
#   when temptable_use_mmap=ON, default OFF, sql/sys_vars.cc:5149 and
#   allocator.h:104). Otherwise it throws RECORD_FILE_FULL
#   (allocator.h:216-234), so with the defaults an exhausted pool converts
#   a table to disk even while it is under tmp_table_size.
# * RECORD_FILE_FULL from a MEMORY or TempTable table makes
#   create_ondisk_from_heap() rebuild it as an on-disk InnoDB table
#   (sql/sql_tmp_table.cc:2595, 2639).
# * MariaDB: tmp_table_size is an alias of tmp_memory_table_size
#   (sql/sys_vars.cc:4510). The HEAP row cap is
#   MY_MIN(tmp_memory_table_size, max_heap_table_size)
#   (sql/sql_select.cc:22453), and create_internal_tmp_table_from_heap()
#   moves the table to TMP_ENGINE_HTON (sql/sql_select.cc:23438): Aria in
#   default builds (sql/sql_class.h:6646; USE_ARIA_FOR_TMP_TABLES defaults
#   ON in storage/maria/CMakeLists.txt:107), MyISAM otherwise.

#: ``tmp_table_size`` default in MySQL 8.4 and MariaDB 11.4 (bytes).
TMP_TABLE_SIZE_DEFAULT = 16 * 1024 * 1024  # 16 MiB

#: ``max_heap_table_size`` default in MySQL 8.4 and MariaDB 11.4 (bytes).
MAX_HEAP_TABLE_SIZE_DEFAULT = 16 * 1024 * 1024  # 16 MiB

#: MySQL TempTable engine (``internal_tmp_mem_storage_engine=TempTable``),
#: the MySQL 8.0+ default.
TMP_ENGINE_TEMPTABLE = "temptable"
#: MySQL MEMORY engine (``internal_tmp_mem_storage_engine=MEMORY``).
TMP_ENGINE_MEMORY = "memory"
#: MariaDB's in-memory (HEAP) internal temporary table.
TMP_ENGINE_MARIADB = "mariadb"
#: Accepted ``engine`` values for :func:`tmp_table_cost`.
TMP_TABLE_ENGINES = (TMP_ENGINE_TEMPTABLE, TMP_ENGINE_MEMORY, TMP_ENGINE_MARIADB)

_TMP_ENGINE_LABEL = {
    TMP_ENGINE_TEMPTABLE: "TempTable",
    TMP_ENGINE_MEMORY: "MEMORY",
    TMP_ENGINE_MARIADB: "MariaDB in-memory (HEAP)",
}
_TMP_ENGINE_DISK = {
    TMP_ENGINE_TEMPTABLE: "InnoDB",
    TMP_ENGINE_MEMORY: "InnoDB",
    TMP_ENGINE_MARIADB: "Aria",
}


class TmpTableCost(NamedTuple):
    effective_limit: int
    fits_in_memory: bool
    memory_rows: int
    disk_rows: int
    explanation: str
    #: One of :data:`TMP_TABLE_ENGINES`.
    engine: str = TMP_ENGINE_TEMPTABLE
    #: Which setting sets ``effective_limit``: ``"tmp_table_size"``,
    #: ``"max_heap_table_size"``, or ``"both"`` when they are equal and both
    #: apply (MEMORY, MariaDB).
    limit_source: str = "tmp_table_size"
    #: Engine of the on-disk table after conversion.
    disk_engine: str = "InnoDB"


def _normalize_tmp_engine(engine: str) -> str:
    key = str(engine or "").strip().lower()
    if key not in TMP_TABLE_ENGINES:
        raise ValueError(
            "engine must be one of {}, got {!r}".format(
                ", ".join(TMP_TABLE_ENGINES), engine,
            )
        )
    return key


def tmp_table_cost(
    rows: int,
    row_size: int,
    tmp_table_size: int = TMP_TABLE_SIZE_DEFAULT,
    max_heap_table_size: int = MAX_HEAP_TABLE_SIZE_DEFAULT,
    engine: str = TMP_ENGINE_TEMPTABLE,
) -> TmpTableCost:
    """Return a cost breakdown for one internal temporary table.

    The in-memory limit depends on ``engine`` (case-insensitive):

    * ``"temptable"`` (default; MySQL 8.0+ ``internal_tmp_mem_storage_engine``
      default): ``tmp_table_size`` per table. ``max_heap_table_size`` does
      not apply. All TempTable tables also share the global
      ``temptable_max_ram`` pool; with the default ``temptable_max_mmap=0``
      an exhausted pool converts a table to disk even below
      ``tmp_table_size``. This model covers the per-table limit only.
    * ``"memory"`` (``internal_tmp_mem_storage_engine=MEMORY``): the smaller
      of ``tmp_table_size`` and ``max_heap_table_size``.
    * ``"mariadb"``: the smaller of the two as well.

    Past the limit, MySQL rebuilds the table as an on-disk InnoDB temporary
    table and MariaDB as an on-disk Aria table. Callers that pass only the
    two sizes get MySQL's default TempTable rule. Source citations are in
    the comment block above.
    """
    engine = _normalize_tmp_engine(engine)
    if engine == TMP_ENGINE_TEMPTABLE:
        effective_limit = tmp_table_size
        limit_source = "tmp_table_size"
        rule = "tmp_table_size alone; max_heap_table_size does not apply to TempTable"
    else:
        effective_limit = min(tmp_table_size, max_heap_table_size)
        if tmp_table_size == max_heap_table_size:
            limit_source = "both"
        elif tmp_table_size < max_heap_table_size:
            limit_source = "tmp_table_size"
        else:
            limit_source = "max_heap_table_size"
        rule = "the smaller of tmp_table_size and max_heap_table_size"
    label = _TMP_ENGINE_LABEL[engine]
    disk_engine = _TMP_ENGINE_DISK[engine]
    if rows <= 0 or row_size <= 0:
        return TmpTableCost(
            effective_limit=effective_limit,
            fits_in_memory=True, memory_rows=0, disk_rows=0,
            explanation="No rows to materialize.",
            engine=engine, limit_source=limit_source, disk_engine=disk_engine,
        )
    memory_capacity = max(1, effective_limit // row_size)
    fits = rows <= memory_capacity
    memory_rows = min(rows, memory_capacity)
    disk_rows = 0 if fits else rows - memory_capacity
    if fits:
        explanation = (
            f"All {rows:,} rows ({rows * row_size:,} bytes) fit in the {label} "
            f"temp table. Its limit is {effective_limit:,} bytes ({rule}). "
            f"No on-disk conversion."
        )
        if engine == TMP_ENGINE_TEMPTABLE:
            explanation += (
                " TempTable tables also share the global temptable_max_ram "
                "pool; if it runs out, a table converts to on-disk InnoDB even "
                "below tmp_table_size."
            )
    else:
        explanation = (
            f"The {label} temp table holds {memory_capacity:,} rows "
            f"({effective_limit:,} bytes: {rule}). Row {memory_capacity + 1:,} "
            f"triggers conversion to an on-disk {disk_engine} temp table, and "
            f"the remaining {disk_rows:,} rows are inserted there. "
        )
        if engine == TMP_ENGINE_TEMPTABLE:
            explanation += (
                "Raise tmp_table_size to avoid the conversion; raising "
                "max_heap_table_size changes nothing here."
            )
        else:
            explanation += (
                "Raise tmp_table_size and max_heap_table_size together; "
                "raising only one leaves the smaller limit in force."
            )
    return TmpTableCost(
        effective_limit=effective_limit,
        fits_in_memory=fits,
        memory_rows=memory_rows,
        disk_rows=disk_rows,
        explanation=explanation,
        engine=engine,
        limit_source=limit_source,
        disk_engine=disk_engine,
    )


# ---------------------------------------------------------------------------
# Index Condition Pushdown (ICP)
# ---------------------------------------------------------------------------


class ICPCost(NamedTuple):
    index_rows_scanned: int
    rows_fetched_without_icp: int
    rows_fetched_with_icp: int
    rows_saved: int
    selectivity_pct: float
    explanation: str


def icp_cost(
    index_rows_scanned: int,
    icp_selectivity: float = 0.20,
) -> ICPCost:
    """Return a cost breakdown for Index Condition Pushdown.

    ``index_rows_scanned``: rows matching the range condition on the
    leading index column (e.g. ``last_name LIKE 'S%'``).

    ``icp_selectivity``: fraction of those rows that also satisfy the
    pushed-down condition on the trailing column (e.g. ``first_name
    LIKE 'J%'``). Default 20% — only 1 in 5 range-matching rows
    actually satisfies the full WHERE clause.

    Without ICP, all ``index_rows_scanned`` rows trigger a clustered-index
    lookup. With ICP, only the matching fraction does.
    """
    if index_rows_scanned <= 0:
        return ICPCost(
            index_rows_scanned=0, rows_fetched_without_icp=0,
            rows_fetched_with_icp=0, rows_saved=0,
            selectivity_pct=icp_selectivity * 100,
            explanation="No rows scanned.",
        )
    without = index_rows_scanned
    with_icp = max(1, int(index_rows_scanned * icp_selectivity))
    saved = without - with_icp
    explanation = (
        f"Without ICP: all {without:,} index rows trigger a clustered-index "
        f"row fetch. With ICP: the storage engine checks the pushed condition "
        f"first — only {with_icp:,} rows ({icp_selectivity * 100:.0f}%) need "
        f"a row fetch. {saved:,} row fetches saved."
    )
    return ICPCost(
        index_rows_scanned=index_rows_scanned,
        rows_fetched_without_icp=without,
        rows_fetched_with_icp=with_icp,
        rows_saved=saved,
        selectivity_pct=icp_selectivity * 100,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Index Merge (union / intersection / sort-union)
# ---------------------------------------------------------------------------


class IndexMergeCost(NamedTuple):
    index_a_rows: int
    index_b_rows: int
    merged_rows: int
    rows_fetched: int
    variant: str
    explanation: str


def index_merge_cost(
    index_a_rows: int,
    index_b_rows: int,
    overlap_pct: float = 0.10,
    variant: str = "union",
) -> IndexMergeCost:
    """Return a cost breakdown for index merge operations.

    ``index_a_rows`` / ``index_b_rows``: rows returned by each index scan.

    ``overlap_pct``: fraction of rows present in both sets (for union
    deduplication or intersection matching).

    ``variant``: ``"union"`` (OR), ``"intersection"`` (AND), or
    ``"sort_union"`` (OR with unsorted row-IDs that need sorting first).
    """
    if index_a_rows <= 0 and index_b_rows <= 0:
        return IndexMergeCost(
            index_a_rows=0, index_b_rows=0, merged_rows=0,
            rows_fetched=0, variant=variant, explanation="No rows scanned.",
        )
    total = index_a_rows + index_b_rows
    overlap = int(min(index_a_rows, index_b_rows) * overlap_pct)

    if variant == "union":
        merged = total - overlap
        explanation = (
            f"Index merge union: scan idx_a ({index_a_rows:,} rows) + "
            f"scan idx_b ({index_b_rows:,} rows). Merge the two sorted "
            f"row-ID streams and de-duplicate — {overlap:,} rows appear in "
            f"both. Result: {merged:,} unique row-IDs fetched from the "
            f"clustered index."
        )
    elif variant == "intersection":
        merged = overlap
        explanation = (
            f"Index merge intersection: scan idx_a ({index_a_rows:,} rows) + "
            f"scan idx_b ({index_b_rows:,} rows). Keep only row-IDs present "
            f"in both — {merged:,} rows. Far fewer clustered-index fetches "
            f"than a full scan."
        )
    elif variant == "sort_union":
        merged = total - overlap
        explanation = (
            f"Index merge sort-union: scan idx_a ({index_a_rows:,} rows) + "
            f"scan idx_b ({index_b_rows:,} rows). Row-IDs are NOT pre-sorted "
            f"(range scans), so MySQL sorts each set first, then merges. "
            f"Result: {merged:,} unique rows fetched."
        )
    else:
        raise ValueError(f"unknown variant: {variant!r}")

    return IndexMergeCost(
        index_a_rows=index_a_rows,
        index_b_rows=index_b_rows,
        merged_rows=merged,
        rows_fetched=merged,
        variant=variant,
        explanation=explanation,
    )
