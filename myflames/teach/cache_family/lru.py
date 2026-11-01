"""Lesson: InnoDB midpoint-insertion LRU vs textbook LRU.

Redesigned as a 3-act story so the scan-resistance property is visceral:

Act 1 -- "The hot set": 8 OLTP pages fill both pools. Blue pages = hot.
Act 2 -- "The scan arrives": 30+ unique scan pages stream through.
         Classic LRU: hot pages are evicted one by one.
         InnoDB: scan pages enter only the old sublist. Young stays put.
Act 3 -- "Hot queries return": the original 8 pages are re-accessed.
         Classic: all misses (they're gone). InnoDB: all hits (still there).

Each act is a separate phase with a pause and a clear label in between.

Concrete sample data: hot pages are labelled with actual table/row names
(e.g. "users:42", "orders:101") and scan pages show "events:1001" etc.,
following the same pattern as hash_join.py.
"""
from .. import _html
from .._cost_model import (
    INNODB_OLD_BLOCKS_PCT_DEFAULT,
    INNODB_OLD_BLOCKS_TIME_DEFAULT_MS,
)


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). lru.js marks the hot set, the
# scan's first page and its quick re-read, the first evictions, each act-3
# read and the result with tl.beat(kind); kinds map to the InnoDB lines
# below. The page-access rules were checked in both servers: a page read
# from disk goes to the old sublist, just behind its first page LRU_old
# (buf_LRU_add_block(bpage, true) in buf0buf.cc / buf0rea.cc), an old page
# moves to the young head only when its first read was
# innodb_old_blocks_time or more ago (buf_page_peek_if_too_old), and a new
# page comes from the free list, with an eviction scan from the LRU tail
# only when that list is empty (buf_LRU_get_free_block, LRUItr::start). The
# MySQL function names are on _html.VERIFIED_SERVER_FUNCTIONS (checked in
# mysql-server 8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "InnoDB page read, midpoint LRU",
        "lines": [
            ("access", 0, "a query needs page P:", "buf_page_get_gen"),
            ("young", 1, "if P is in the young sublist:"),
            ("young-hit", 2, "hit: P is in memory, no disk read"),
            ("old", 1, "elif P is in the old sublist:"),
            ("old-time", 2, "if P was first read >= innodb_old_blocks_time ago:",
             "buf_page_peek_if_too_old"),
            ("promote", 3, "move P to the head of the young sublist",
             "buf_LRU_make_block_young"),
            ("old-stay", 2, "else: leave P in the old sublist"),
            ("miss", 1, "else, a miss:"),
            ("full", 2, "if no free page is left:"),
            ("evict", 3, "evict the page at the LRU tail, the least recently used",
             "buf_LRU_free_from_common_LRU_list"),
            ("insert", 2, "read P from disk into the old sublist (the midpoint)",
             "buf_LRU_add_block"),
        ],
    },
    "beats": {
        "hot-set": ["access", "old", "old-time", "promote"],
        "scan-miss": ["access", "miss", "insert"],
        "scan-reread": ["access", "old", "old-time", "old-stay"],
        "scan-evict": ["access", "miss", "full", "evict", "insert"],
        "return-young": ["access", "young", "young-hit"],
        "return-old": ["access", "old", "old-time", "promote"],
        "return-miss": ["access", "miss", "full", "evict", "insert"],
        "return-miss-room": ["access", "miss", "insert"],
        "summary": ["insert", "old-stay"],
    },
    "quiz": [
        {
            "question": ("A full scan reads events:1001 from disk while your hot pages sit in the "
                         "young sublist. Where does InnoDB put events:1001?"),
            "choices": [
                ("In the old sublist, at the midpoint about 3/8 of the way up from the tail", True,
                 "A page read from disk enters the old sublist, which keeps innodb_old_blocks_pct "
                 "(37 by default) of the pages, so the young sublist is not disturbed."),
                ("At the head of the list, like a textbook LRU", False,
                 "That is how a scan pushes hot pages out of a textbook LRU; InnoDB inserts new "
                 "pages at the midpoint instead."),
                ("In a separate buffer used only by scans", False,
                 "Scan pages share the same LRU list as every other page; they just enter it in "
                 "the old sublist."),
            ],
        },
        {
            "question": ("The scan reads the next rows of events:1001 a few milliseconds after loading "
                         "it. Why does the page stay in the old sublist?"),
            "choices": [
                ("Its first read was less than innodb_old_blocks_time (1,000 ms) ago", True,
                 "An old page moves to the young sublist only when it is read again at least "
                 "innodb_old_blocks_time after its first read, and a scan is done with a page "
                 "within milliseconds."),
                ("InnoDB never moves a page out of the old sublist", False,
                 "It does: a page read again 1,000 ms or more after its first read moves to the "
                 "head of the young sublist, which is how hot pages get there."),
                ("Pages read by a full scan are read-only", False,
                 "Read-only has nothing to do with it; the time since the page's first read "
                 "decides whether it moves."),
            ],
        },
        {
            "question": ("In this lesson you raise innodb_old_blocks_pct from 37 to 90. Why do most "
                         "hot pages now miss in act 3?"),
            "choices": [
                ("The young sublist shrinks to about a tenth of the pool, so most hot pages sit in "
                 "the old sublist where the scan evicts them", True,
                 "innodb_old_blocks_pct is the old sublist's share of the list, so 90 leaves the "
                 "young sublist room for only 2 of the 20 pages here."),
                ("The scan now skips the old sublist", False,
                 "Pages read from disk still enter at the midpoint; only the size of each sublist "
                 "changed."),
                ("InnoDB stops evicting pages", False,
                 "The old sublist is still full during the scan, so every new page still evicts "
                 "the page at its tail."),
            ],
        },
    ],
}


def render() -> str:
    controls_html = f"""
<section class="controls">
  <h2>Parameters (InnoDB buffer pool)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="pool_size">Buffer pool (pages): <span class="value-pill" data-pill-for="pool_size">20</span></label>
      <input type="range" id="pool_size" name="pool_size" min="12" max="40" step="2" value="20">
      <div class="hint">For illustration. Real pools are millions of pages.</div>
    </div>

    <div class="control">
      <label for="old_pct">innodb_old_blocks_pct: <span class="value-pill" data-pill-for="old_pct">{INNODB_OLD_BLOCKS_PCT_DEFAULT}</span></label>
      <input type="range" id="old_pct" name="old_pct" min="10" max="90" step="1" value="{INNODB_OLD_BLOCKS_PCT_DEFAULT}">
      <div class="hint">% of pool reserved for the old (cold) sublist. Default {INNODB_OLD_BLOCKS_PCT_DEFAULT}.</div>
    </div>

    <div class="control">
      <label for="scan_pages">Scan pages (act 2): <span class="value-pill" data-pill-for="scan_pages">30</span></label>
      <input type="range" id="scan_pages" name="scan_pages" min="10" max="80" step="5" value="30">
      <div class="hint">How many unique pages the reporting query scans.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Act 1: Your OLTP workload keeps 8 hot pages in the pool\n"
            "SELECT * FROM users WHERE id = 42;   -- repeated point lookups\n\n"
            "-- Act 2: A reporting query runs a full table scan\n"
            "SELECT SUM(amount) FROM events WHERE event_date >= '2026-01-01';\n\n"
            "-- Act 3: OLTP workload returns — same 8 pages\n"
            "SELECT * FROM users WHERE id = 42;   -- hit or miss?"
        ),
        note="Watch what happens to the 8 blue pages during the scan."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation: a 3-act story",
        [
            "Each cell is one 16 KiB page, labelled table:page (users:42, events:1001). Blue = your hot OLTP pages, orange = pages the reporting scan reads, green = a hit in act 3, red = a miss read from disk again.",
            "Act 1, 'The hot set': 8 blue OLTP pages fill both pools. Each was read again more than innodb_old_blocks_time (1,000 ms) after its first read, so InnoDB keeps it in the young sublist.",
            "Act 2, 'The scan arrives': orange events pages stream in. In the textbook LRU (right) every new page goes to the head and pushes the blue pages toward eviction. In InnoDB (left) a page read from disk enters the old sublist at the midpoint, and the scan's quick re-reads of the same page leave it there.",
            "Act 3, 'Hot queries return': the same 8 blue pages are read again. With the default settings the textbook LRU misses all 8 (they were evicted) and InnoDB hits all 8. Raise innodb_old_blocks_pct and the young sublist gets too small for the hot set.",
            "The drawing keeps both sublists at fixed sizes, as in a full pool: once the old sublist has no empty cell, no free page is left, so each read from disk evicts the page at the tail. The line under each pool says what the last step did; the counters track evictions and hits.",
            "Next step plays one step at a time; the simplified code beside the drawing highlights the InnoDB lines each step runs.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
  <div class="stage-with-phases">
    <div style="flex:1;min-width:0;display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div>
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:var(--svg-token);letter-spacing:0.4px;text-transform:uppercase">InnoDB midpoint-insertion LRU</p>
        <svg id="svg-innodb" viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg"></svg>
      </div>
      <div>
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:var(--svg-ink-muted);letter-spacing:0.4px;text-transform:uppercase">Textbook single-list LRU</p>
        <svg id="svg-classic" viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg"></svg>
      </div>
    </div>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Simulation results</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">InnoDB: young pages {ht("Pages in the hot (MRU) half. These survive a full scan because new pages only enter the old sublist.")}</p><p class="value" id="out-young">—</p></div>
    <div class="item"><p class="label">InnoDB: evictions {ht("Pages kicked out of the old sublist tail. During a scan these are scan pages, not your hot pages.")}</p><p class="value" id="out-evictions">—</p></div>
    <div class="item"><p class="label">Act 3 — InnoDB hits {ht("When the hot queries return in Act 3, how many find their page still in the pool. 8/8 with the default settings: the scan never reached the young sublist.")}</p><p class="value ok" id="out-innodb-hits">—</p></div>
    <div class="item"><p class="label">Classic LRU: evictions {ht("Pages kicked out during the scan. In a textbook LRU, these are your hot OLTP pages — the scan pushed them all out.")}</p><p class="value" id="out-classic-evictions">—</p></div>
    <div class="item"><p class="label">Act 3 — Classic hits {ht("When the hot queries return, how many find their page. 0/8 after a scan larger than the pool: they were all evicted.")}</p><p class="value hot" id="out-classic-hits">—</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
</section>
"""

    learn_more_html = f"""
<details class="learn-more">
  <summary>Learn more — why is a full table scan a problem for plain LRU?</summary>
  <div class="body">
    <p>Imagine a 1 GB buffer pool and a 10 GB table. Under <strong>textbook
    LRU</strong>, reading the whole table once visits every page exactly
    once — and each visit bumps that page to the head of the list. By the
    end of the scan, every single page that used to be hot has been
    evicted. Your OLTP working set just got destroyed by a reporting query.</p>

    <p>InnoDB's answer is <strong>midpoint-insertion LRU</strong>:</p>
    <ol>
      <li>The linked list is split into a <em>young</em> sublist (MRU end,
      ~5/8) and an <em>old</em> sublist (LRU end, ~3/8). The split is
      <code>innodb_old_blocks_pct</code> (default {INNODB_OLD_BLOCKS_PCT_DEFAULT}).</li>

      <li>On a <strong>cache miss</strong>, the new page enters at the
      <em>midpoint</em>, NOT the head of the list: InnoDB links it just
      behind the first page of the old sublist (<code>LRU_old</code>); the
      drawing simplifies that to the head of the old sublist. (A list
      shorter than 512 pages, 8 MiB of 16 KiB pages, has no old sublist
      yet, so there new pages go to the head; real pools are far larger.)</li>

      <li>On a <strong>hit in old</strong>, the page only promotes to young if
      <code>now - first_access ≥ innodb_old_blocks_time</code> (default
      {INNODB_OLD_BLOCKS_TIME_DEFAULT_MS} ms). A scan reads all the rows of a
      page within milliseconds, so its re-reads never trigger this: scan
      pages cycle through old and never pollute young.</li>
    </ol>

    <p>MySQL and MariaDB share this design: both read a page into the old
    sublist (<code>buf_LRU_add_block</code> with <code>old</code> set),
    promote an old page only after <code>innodb_old_blocks_time</code>
    (<code>buf_page_peek_if_too_old</code>), and take the page for a read
    from the free list, evicting from the tail of the LRU list only when no
    free page is left (<code>buf_LRU_get_free_block</code>).</p>

    <p>Sources: MySQL 8.4 Reference Manual §17.5.1 "Buffer Pool" and
    §17.8.3.3 "Making the Buffer Pool Scan Resistant"; InnoDB source
    <code>storage/innobase/buf/buf0lru.cc</code> in both servers.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="lru",
        title="InnoDB buffer pool: midpoint-insertion LRU",
        subtitle=(
            "A 3-act story: your hot pages, a full-table scan, and the "
            "moment you discover whether the scan wiped your cache."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
        guide=GUIDE,
    )
