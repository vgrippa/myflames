"""Lesson: Why cold queries are slow — the InnoDB buffer pool warmup story.

Three acts:

Act 1 — **Cold start**. The buffer pool is empty. Every page the query
        asks for has to be read from disk. Bars are long (disk latency).

Act 2 — **Hit ratio climbs**. The same query runs again. Pages are now
        in the pool, so reads are in-memory and ~50–100× faster.

Act 3 — **Warm restart** (dump/load). On shutdown, InnoDB writes the
        hottest N% of the pool to ``ib_buffer_pool``. On startup it
        loads that file back asynchronously — so a restart doesn't
        leave you cold.

MySQL internals facts referenced here are verified against the server
source tree (storage/innobase/buf/buf0dump.cc and
storage/innobase/handler/ha_innodb.cc around line 22688):

  * ``innodb_buffer_pool_dump_pct`` default is **25** — only the top
    25% of hot pages get serialized. This keeps dump time bounded.
  * Dump file name is **``ib_buffer_pool``** (constant
    ``SRV_BUF_DUMP_FILENAME_DEFAULT`` in storage/innobase/include/srv0srv.h).
  * ``SET GLOBAL innodb_buffer_pool_load_now=ON`` wakes the background
    dump/load thread and returns immediately — the load runs async.
  * ``innodb_buffer_pool_load_at_startup`` (default ON) kicks off the
    same load once InnoDB is up.
"""
from .. import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render():
    query_card_html = _html.query_card(
        "SELECT u.id, u.email, o.total "
        "FROM users u JOIN orders o ON o.user_id = u.id "
        "WHERE o.created_at > NOW() - INTERVAL 7 DAY",
        note=(
            "Same query, two runs back-to-back. Act 1 reads from disk; "
            "Act 2 reads from RAM. The third act shows what happens "
            "when the server restarts — warm, not cold."
        ),
    )
    explainer_html = _html.explainer(
        "What you'll see",
        [
            "Cells = buffer-pool slots. Red-ish = just read from disk (slow). "
            "Green = pool hit (fast). Yellow = dumped to <code>ib_buffer_pool</code> "
            "on shutdown.",
            "Latency counter on the right updates live as each page is touched.",
            "The hit/miss ratio is what decides whether a repeat run is "
            "milliseconds (warm) or seconds (cold).",
        ],
    )
    controls_html = f"""
<section class="controls" aria-labelledby="controls-heading">
  <h2 id="controls-heading" class="visually-hidden">Controls</h2>
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play to warm the buffer pool")}
</section>
"""
    stage_html = f"""
<section class="stage">
  <div class="stage-with-phases">
    <div style="flex:1;min-width:0">
      <svg id="svg-pool" viewBox="0 0 500 260" xmlns="http://www.w3.org/2000/svg"></svg>
    </div>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Live cache stats</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Phase</p><p class="value" id="out-phase">—</p></div>
    <div class="item"><p class="label">Pool hits {ht("Pages found in the buffer pool — no disk I/O.")}</p><p class="value ok" id="out-hits">—</p></div>
    <div class="item"><p class="label">Pool misses {ht("Pages not in the pool — read from storage and inserted.")}</p><p class="value hot" id="out-misses">—</p></div>
    <div class="item"><p class="label">Cumulative I/O time {ht("Sum of per-page latencies at ~6 ms for a cold read, ~0.06 ms for a warm one. Real SSD ratios vary but the orders of magnitude are correct.")}</p><p class="value" id="out-total-ms">—</p></div>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — the dump &amp; load mechanism</summary>
  <div class="body">
    <p>A cold-start disaster is the first thing a DBA learns to avoid:
    restart the server, and the first hour of queries all go to disk
    instead of RAM because the buffer pool is empty. InnoDB ships with
    two defaults that make this a solved problem — but only if you
    know they exist.</p>

    <h3>Dump on shutdown</h3>
    <ul>
      <li><code>innodb_buffer_pool_dump_at_shutdown</code> (ON by
      default) writes the identities of the hottest
      <code>innodb_buffer_pool_dump_pct</code> = <strong>25</strong>
      percent of pages to a small file named
      <code>ib_buffer_pool</code> in the data directory.</li>
      <li>Only <em>page IDs</em> are dumped, not page data — the file
      is tiny (a few MB for a 10 GB pool).</li>
    </ul>

    <h3>Load on startup</h3>
    <ul>
      <li><code>innodb_buffer_pool_load_at_startup</code> (ON) reads
      <code>ib_buffer_pool</code> and issues reads for those pages.
      The load runs in a <em>background thread</em>; queries don't
      block on it.</li>
      <li><code>SET GLOBAL innodb_buffer_pool_load_now = ON</code>
      triggers the same load immediately at runtime. The statement
      returns fast — the actual reads run async (verified in
      <code>storage/innobase/buf/buf0dump.cc</code>'s
      <code>buf_load_start()</code>).</li>
    </ul>

    <h3>Common sizing mistake</h3>
    <p>If <code>innodb_buffer_pool_size</code> is small relative to
    your working set, no amount of warmup helps — pages you loaded at
    startup get evicted before real queries reach them. Size the pool
    to the working set first; <em>then</em> worry about warmup.</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="buffer_pool_warmup",
        title="InnoDB buffer pool — the cold-start problem &amp; dump/load cure",
        subtitle=(
            "Same query, run twice: why the first run hits disk and "
            "the second runs from RAM — plus the restart story."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS_TEMPLATE,
    )
