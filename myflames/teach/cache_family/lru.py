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
            "-- Act 1: Your OLTP workload keeps 8 hot pages in the pool\\n"
            "SELECT * FROM users WHERE id = 42;   -- repeated point lookups\\n\\n"
            "-- Act 2: A reporting query runs a full table scan\\n"
            "SELECT SUM(amount) FROM events WHERE event_date >= '2026-01-01';\\n\\n"
            "-- Act 3: OLTP workload returns — same 8 pages\\n"
            "SELECT * FROM users WHERE id = 42;   -- hit or miss?"
        ),
        note="Watch what happens to the 8 blue pages during the scan."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation — a 3-act story",
        [
            "Act 1 — 'The hot set': 8 blue OLTP pages fill both pools. These are your frequently-accessed users, orders, products rows.",
            "Act 2 — 'The scan arrives': orange scan pages stream in. In the textbook LRU (right), they push the blue pages out. In InnoDB (left), scan pages only enter the OLD sublist — the blue young pages don't move.",
            "Act 3 — 'Hot queries return': the same 8 blue pages are re-accessed. Classic LRU: all 8 are cache MISSES (they were evicted during the scan). InnoDB: all 8 are cache HITS (they never left the young sublist).",
            "A counter at the bottom tracks misses vs hits. After act 3, the difference is the whole argument for InnoDB's LRU design.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play to start the 3-act story")}
  <div class="stage-with-phases">
    <div style="flex:1;min-width:0;display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div>
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#1e40af;letter-spacing:0.4px;text-transform:uppercase">InnoDB midpoint-insertion LRU</p>
        <svg id="svg-innodb" viewBox="0 0 400 300" xmlns="http://www.w3.org/2000/svg"></svg>
      </div>
      <div>
        <p style="margin:0 0 6px;font-size:12px;font-weight:700;color:#6b7280;letter-spacing:0.4px;text-transform:uppercase">Textbook single-list LRU</p>
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
    <div class="item"><p class="label">Act 3 — InnoDB hits {ht("When the hot queries return in Act 3, how many find their page still in the pool. Should be 8/8 after a scan.")}</p><p class="value ok" id="out-innodb-hits">—</p></div>
    <div class="item"><p class="label">Classic LRU: evictions {ht("Pages kicked out during the scan. In a textbook LRU, these are your hot OLTP pages — the scan pushed them all out.")}</p><p class="value" id="out-classic-evictions">—</p></div>
    <div class="item"><p class="label">Act 3 — Classic hits {ht("When the hot queries return, how many find their page. Should be 0/8 after a scan — they were all evicted.")}</p><p class="value hot" id="out-classic-hits">—</p></div>
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
      <em>midpoint</em> (head of old), NOT the head of the list.</li>

      <li>On a <strong>hit in old</strong>, the page only promotes to young if
      <code>now - first_access ≥ innodb_old_blocks_time</code> (default
      {INNODB_OLD_BLOCKS_TIME_DEFAULT_MS} ms). A one-pass scan never triggers
      this — so scan pages cycle through old and never pollute young.</li>
    </ol>

    <p>Sources: MySQL 8.4 Reference Manual §17.5.1 "Buffer Pool" and
    §17.8.3.3 "Making the Buffer Pool Scan Resistant".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="lru",
        title="InnoDB buffer pool — midpoint-insertion LRU",
        subtitle=(
            "A 3-act story: your hot pages, a full-table scan, and the "
            "moment you discover whether the scan wiped your cache."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
