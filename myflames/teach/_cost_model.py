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
from typing import Dict, NamedTuple

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
