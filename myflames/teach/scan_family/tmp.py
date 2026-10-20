"""Lesson: Temporary tables — in-memory to on-disk conversion.

Animates how GROUP BY / DISTINCT / UNION materialize rows into an
in-memory internal temporary table, and what happens when the data
outgrows the table's limit: the conversion to an on-disk table.

The limit depends on the engine (see ``tmp_table_cost`` in
``_cost_model.py`` for the MySQL 8.4 / MariaDB 11.4 source citations):

* MySQL TempTable (the 8.0+ default): ``tmp_table_size`` per table.
  ``max_heap_table_size`` does not apply. All TempTable tables also share
  the global ``temptable_max_ram`` pool.
* MySQL MEMORY (``internal_tmp_mem_storage_engine=MEMORY``): the smaller
  of ``tmp_table_size`` and ``max_heap_table_size``.
* MariaDB: the smaller of the two as well, spilling to Aria instead of
  InnoDB.

Concrete sample data: employees grouped by department, counting
headcount per department.
"""
from .. import _html
from .._cost_model import TMP_TABLE_SIZE_DEFAULT, MAX_HEAP_TABLE_SIZE_DEFAULT


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = f"""
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (internal temporary tables, MySQL 8.4 and MariaDB 11.4)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="rows">Rows to materialize: <span class="value-pill" data-pill-for="rows">10000</span></label>
      <input type="range" id="rows" name="rows" min="100" max="1000000" step="100" value="10000">
      <div class="hint">Total rows inserted into the internal temp table.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="8" value="200">
      <div class="hint">Size of each materialized row in the temp table.</div>
    </div>

    <div class="control">
      <label for="engine">In-memory temp table engine</label>
      <select id="engine" name="engine">
        <option value="temptable" selected>MySQL TempTable (8.4 default)</option>
        <option value="memory">MySQL MEMORY</option>
        <option value="mariadb">MariaDB (spills to Aria)</option>
      </select>
      <div class="hint">On MySQL this is <code>internal_tmp_mem_storage_engine</code>. It decides which size limit applies.</div>
    </div>

    <div class="control">
      <label for="tmp_size">tmp_table_size (bytes): <span class="value-pill" data-pill-for="tmp_size">{TMP_TABLE_SIZE_DEFAULT}</span></label>
      <input type="range" id="tmp_size" name="tmp_size" min="65536" max="134217728" step="65536" value="{TMP_TABLE_SIZE_DEFAULT}">
      <div class="hint">Default: 16 MiB. Limits each in-memory temp table on every engine.</div>
    </div>

    <div class="control" id="max-heap-control" style="transition: opacity 200ms ease-out">
      <label for="max_heap">max_heap_table_size (bytes): <span class="value-pill" data-pill-for="max_heap">{MAX_HEAP_TABLE_SIZE_DEFAULT}</span></label>
      <input type="range" id="max_heap" name="max_heap" min="65536" max="134217728" step="65536" value="{MAX_HEAP_TABLE_SIZE_DEFAULT}">
      <div class="hint" id="max-heap-hint">Default: 16 MiB. Used only by MEMORY and MariaDB, where the smaller of the two sizes wins. TempTable ignores it.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- GROUP BY without index → internal temp table\n"
            "SELECT   department, COUNT(*) AS headcount\n"
            "FROM     employees\n"
            "GROUP BY department\n"
            "HAVING   COUNT(*) > 5;"
        ),
        note=(
            "No index matches GROUP BY department, so the server groups the rows "
            "in an internal temporary table. The table starts in memory and moves "
            "to disk if it outgrows its size limit."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Each pill is a sample employees row such as \"Alice (Engineering)\". "
            "The 12 pills each stand for one twelfth of the rows you set above.",
            "Green box: the in-memory temp table. Its bar fills toward the size "
            "limit printed above it on the right.",
            "That limit depends on the engine. MySQL's default, TempTable, uses "
            "<code>tmp_table_size</code> alone. The MEMORY engine and MariaDB use "
            "the smaller of <code>tmp_table_size</code> and <code>max_heap_table_size</code>.",
            "At 100%, the table is copied into the red box: an on-disk table "
            "(InnoDB on MySQL, Aria on MariaDB). Red pills are rows inserted after "
            "that conversion.",
            "Try it: set max_heap_table_size below tmp_table_size, then switch the "
            "engine and watch the limit move (or not).",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="tmp-svg" viewBox="0 0 800 380" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (internal temporary tables)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Effective limit {ht("The most this one temp table can hold in memory. TempTable (MySQL's default) uses tmp_table_size. The MEMORY engine and MariaDB use the smaller of tmp_table_size and max_heap_table_size.")}</p><p class="value" id="out-limit">—</p></div>
    <div class="item"><p class="label">Limit set by {ht("The setting to raise if you want this table to stay in memory. With MEMORY or MariaDB, raising the larger setting changes nothing: raise the smaller one, or both.")}</p><p class="value" id="out-source">—</p></div>
    <div class="item"><p class="label">In-memory capacity (rows) {ht("How many rows fit under the limit before the conversion to disk starts: the limit divided by the row size.")}</p><p class="value" id="out-cap">—</p></div>
    <div class="item"><p class="label">Rows inserted in memory {ht("Rows inserted while the temp table is still in memory. These are fast: no disk I/O.")}</p><p class="value" id="out-mem-rows">—</p></div>
    <div class="item"><p class="label">Rows inserted on disk {ht("Rows inserted after the table outgrew its limit and moved to an on-disk table (InnoDB on MySQL, Aria on MariaDB).")}</p><p class="value" id="out-disk-rows">—</p></div>
    <div class="item"><p class="label">Disk conversion? {ht("Yes means the table outgrew its limit. The server then copies every row already in memory into a new on-disk table, which is slow.")}</p><p class="value ok" id="out-conversion">—</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — which limit applies, and when does MySQL use internal temp tables?</summary>
  <div class="body">
    <p><strong>Which limit applies.</strong> MySQL 8.4 keeps internal temp tables
    in the <strong>TempTable</strong> engine by default
    (<code>internal_tmp_mem_storage_engine=TempTable</code>). Since MySQL 8.0.28,
    each TempTable table may use up to <code>tmp_table_size</code>;
    <code>max_heap_table_size</code> does not apply. With
    <code>internal_tmp_mem_storage_engine=MEMORY</code>, the limit is the smaller
    of <code>tmp_table_size</code> and <code>max_heap_table_size</code>. MariaDB
    also uses the smaller of the two (there, <code>tmp_table_size</code> is an
    alias of <code>tmp_memory_table_size</code>).</p>
    <p><strong>The shared TempTable pool.</strong> All TempTable tables on the
    server also share one RAM budget, <code>temptable_max_ram</code> (MySQL 8.4
    default: 3% of physical memory, at least 1 GiB and at most 4 GiB; 1 GiB on
    8.0 to 8.3 and on 9.6+ unless <code>server_memory</code> is set). Once it is
    used up, TempTable may continue in memory-mapped files only when
    <code>temptable_use_mmap=ON</code> (default OFF in 8.4, removed in 9.4) and
    only up to <code>temptable_max_mmap</code> (default 0). So with default
    settings a busy server can push a table to disk even while that table is
    under <code>tmp_table_size</code>.</p>
    <p><strong>Where it spills.</strong> MySQL rebuilds the table as an on-disk
    InnoDB temporary table and copies the rows already in memory into it.
    MariaDB rebuilds it as an on-disk Aria table (MyISAM only in builds
    compiled with <code>USE_ARIA_FOR_TMP_TABLES=OFF</code>).</p>
    <p><strong>Straight to disk.</strong> Some tables skip memory entirely. On
    MySQL that happens with <code>big_tables=ON</code> (unless the query uses
    <code>SQL_SMALL_RESULT</code>), and, with the MEMORY engine only, for tables
    with BLOB or TEXT columns. TempTable keeps BLOB and TEXT in memory. MariaDB
    sends tables with BLOB or TEXT columns straight to Aria.</p>
    <hr>
    <p>Queries that create internal temporary tables:</p>
    <p><strong>GROUP BY / DISTINCT / ORDER BY with GROUP BY</strong>: when no
    index produces rows in the grouped or sorted order. This is the most
    common trigger.</p>
    <p><strong>UNION / UNION DISTINCT</strong>: the de-duplication step
    materializes into a temp table. <code>UNION ALL</code> does not need one.</p>
    <p><strong>Derived tables and CTEs</strong>: subqueries in FROM clauses
    and non-recursive CTEs are often materialized.</p>
    <p><strong>Semijoin materialization</strong>: the optimizer may
    materialize the inner side of <code>IN (SELECT ...)</code> into a temp
    table and probe it for each outer row.</p>
    <p>Sources: MySQL 8.4 source <code>sql/sql_tmp_table.cc</code> (row cap per
    engine, <code>create_ondisk_from_heap</code>),
    <code>storage/temptable/include/temptable/allocator.h</code> (per-table and
    shared limits), <code>sql/sys_vars.cc</code> (defaults); MariaDB 11.4
    <code>sql/sql_select.cc</code> (in-memory cap, conversion to Aria); MySQL 8.4
    reference manual §10.4.4 "Internal Temporary Table Use in MySQL".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % (TMP_TABLE_SIZE_DEFAULT, MAX_HEAP_TABLE_SIZE_DEFAULT)

    return _html.render_page(
        lesson_id="tmp",
        title="Temporary tables: in-memory to on-disk conversion",
        subtitle=(
            "Watch a GROUP BY fill an in-memory temp table, hit its size limit, "
            "and convert to an on-disk table. That cliff is why your query "
            "suddenly slows down, and the engine decides where the cliff is."
        ),
        version_chip="MySQL 8.4 • MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
