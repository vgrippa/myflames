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

# Guided steps (see _html.normalize_guide). buffer_pool_warmup.js marks the
# first cold read, the other cold reads, the first warm hit, the other hits,
# then shutdown, restart and startup with tl.beat(kind). Checked in both
# servers: buf_dump() lists the first innodb_buffer_pool_dump_pct of each LRU
# list from its most recently used end as "space,page" lines (MySQL: that
# share of the pages in the list; MariaDB: at most that share of the pool's
# size), and buf_load() reads them back in the background thread
# (buf_dump_thread in MySQL, the buf_dump_load_func task in MariaDB). The
# MySQL function names are on _html.VERIFIED_SERVER_FUNCTIONS (checked in
# mysql-server 8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Buffer pool reads, dump and load",
        "lines": [
            ("run", 0, "for each page P the query needs:"),
            ("check", 1, "if P is in the buffer pool:", "buf_page_get_gen"),
            ("hit", 2, "hit: read the row from memory"),
            ("miss", 1, "else, a miss:"),
            ("read", 2, "read P from disk into the pool, then read the row", "buf_read_page"),
            ("shutdown", 0, "at shutdown, if innodb_buffer_pool_dump_at_shutdown:"),
            ("dump", 1, "write IDs of the most recently used dump_pct% to ib_buffer_pool",
             "buf_dump"),
            ("restart", 0, "restart: the buffer pool starts empty"),
            ("startup", 0, "at startup, if innodb_buffer_pool_load_at_startup:"),
            ("load", 1, "a background thread reads the listed pages into the pool",
             "buf_load"),
        ],
    },
    "beats": {
        "cold-read": ["run", "check", "miss", "read"],
        "warm-hit": ["run", "check", "hit"],
        "dump": ["shutdown", "dump"],
        "restart": ["restart"],
        "load": ["startup", "load"],
    },
    "quiz": [
        {
            "question": "What does InnoDB write to ib_buffer_pool at shutdown?",
            "choices": [
                ("The tablespace ID and page number of each listed page", True,
                 "Each line is a space ID and a page number, so the file stays small and "
                 "the pages themselves are read from the data files again after the restart."),
                ("A copy of each page's contents", False,
                 "Only page IDs are written; the load still reads every listed page from disk, "
                 "just before your queries need it."),
                ("The rows your queries changed", False,
                 "Changes go to the redo log and the data files; ib_buffer_pool only lists "
                 "pages to read back."),
            ],
        },
        {
            "question": ("The second run of the same query needed a fraction of the first run's "
                         "I/O time. Why?"),
            "choices": [
                ("Its pages were already in the buffer pool, so nothing was read from disk", True,
                 "A hit reads the page from memory; only a miss waits for the disk."),
                ("The server cached the first run's result", False,
                 "MySQL 8.0 removed the query cache and MariaDB ships with query_cache_type=OFF, "
                 "so the rows were read again, from pages in memory."),
                ("The first run left the table sorted, so the second run read fewer pages", False,
                 "Both runs read the same 10 pages; the difference is whether each came from "
                 "memory or from disk."),
            ],
        },
        {
            "question": ("Right after a restart you run SET GLOBAL innodb_buffer_pool_load_now = ON. "
                         "What happens?"),
            "choices": [
                ("The statement returns at once and the listed pages are read in the background", True,
                 "It only signals the load to start, so queries keep running while "
                 "Innodb_buffer_pool_load_status reports the progress."),
                ("The statement waits until every listed page is in memory", False,
                 "The load runs in the background, which is why it is safe to start on a busy "
                 "server."),
                ("It reads the whole data directory into memory", False,
                 "It reads only the pages listed in ib_buffer_pool, which with the default "
                 "innodb_buffer_pool_dump_pct covers at most a quarter of the pool."),
            ],
        },
    ],
}


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
            "Cells = buffer-pool slots, each one 16 KiB page labelled table:page "
            "(U:1001 is users page 1001). Red-ish = just read from disk (slow). "
            "Green = pool hit (fast). Yellow = listed in <code>ib_buffer_pool</code> "
            "at shutdown.",
            "The line under the pool says what the last page read did; the readout "
            "below counts hits, disk reads and I/O time for the current run.",
            "Act 3 shuts the server down and restarts it: the file box shows the page "
            "IDs InnoDB saved, and startup reads those pages back in the background. "
            "MySQL saves the most recently used 25% of the cached pages; MariaDB saves "
            "up to 25% of the pool's size. The parameters above change the percentage "
            "and switch between the two rules.",
            "The hit/miss ratio is what decides whether a repeat run is "
            "milliseconds (warm) or seconds (cold). The latencies here are "
            "illustrative: 6 ms per disk read, 0.06 ms per hit.",
            "Next step plays one page read, or one stage of the restart, at a time; "
            "the simplified code beside the drawing highlights the lines each step runs.",
        ],
    )
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (dump at shutdown)</h2>
  <div class="control-grid">
    <div class="control">
      <label for="dump_pct">innodb_buffer_pool_dump_pct: <span class="value-pill" data-pill-for="dump_pct">25</span>%</label>
      <input type="range" id="dump_pct" name="dump_pct" min="1" max="100" step="1" value="25">
      <div class="hint">Share of pages listed in <code>ib_buffer_pool</code> at shutdown. Default 25 in both servers.</div>
    </div>
    <div class="control">
      <label for="server">Server rule for the dump</label>
      <select id="server" name="server">
        <option value="mysql" selected>MySQL: that share of the cached pages</option>
        <option value="mariadb">MariaDB: at most that share of the pool's size</option>
      </select>
      <div class="hint">Both list the most recently used pages first.</div>
    </div>
  </div>
</section>
"""
    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
  <div class="stage-with-phases">
    <svg id="svg-pool" viewBox="0 0 500 244" xmlns="http://www.w3.org/2000/svg"></svg>
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
    <div class="item"><p class="label">I/O time this run {ht("Sum of per-page latencies for the current run: 6 ms for each page read from disk, 0.06 ms for each pool hit. Illustrative numbers: a spinning disk takes milliseconds per read, an SSD about a tenth of a millisecond, and memory far less; the gap between disk and memory is what matters.")}</p><p class="value" id="out-total-ms">—</p></div>
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
      default) writes the identities of the most recently used pages,
      read from the young end of the LRU list, to a small file named
      <code>ib_buffer_pool</code> in the data directory.
      <code>innodb_buffer_pool_dump_pct</code> = <strong>25</strong> by
      default: MySQL lists 25% of the pages in each buffer pool's LRU
      list, MariaDB at most 25% of the pool's size. A shutdown with
      <code>innodb_fast_shutdown = 2</code> skips the dump in both.</li>
      <li>Only <em>page IDs</em> are dumped, not page data — the file
      is tiny (a few MB for a 10 GB pool).</li>
    </ul>

    <h3>Load on startup</h3>
    <ul>
      <li><code>innodb_buffer_pool_load_at_startup</code> (ON) reads
      <code>ib_buffer_pool</code> and issues reads for those pages.
      The load runs in the background (MySQL's dump/load thread,
      MariaDB's dump/load task); queries don't block on it. Both sort
      the list by tablespace and page first, and MySQL keeps the load to
      about <code>innodb_io_capacity</code> reads per second while other
      work is running.</li>
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
        title="InnoDB buffer pool: the cold-start problem & dump/load cure",
        subtitle=(
            "Same query, run twice: why the first run hits disk and "
            "the second runs from RAM — plus the restart story."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS_TEMPLATE,
        guide=GUIDE,
    )
