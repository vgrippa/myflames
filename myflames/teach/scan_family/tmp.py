"""Lesson: Temporary tables — MEMORY to on-disk InnoDB conversion.

Animates how GROUP BY / DISTINCT / UNION materialize rows into a
MEMORY temp table, and what happens when the data exceeds
``min(tmp_table_size, max_heap_table_size)`` — the dramatic conversion
to an on-disk InnoDB table.

Concrete sample data: employees grouped by department, counting
headcount per department.
"""
from .. import _html
from .._cost_model import TMP_TABLE_SIZE_DEFAULT, MAX_HEAP_TABLE_SIZE_DEFAULT


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = f"""
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (MySQL 8.4 internal temporary tables)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="rows">Rows to materialize: <span class="value-pill" data-pill-for="rows">10000</span></label>
      <input type="range" id="rows" name="rows" min="100" max="1000000" step="100" value="10000">
      <div class="hint">Total rows inserted into the internal temp table.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
      <div class="hint">Size of each materialized row in the temp table.</div>
    </div>

    <div class="control">
      <label for="tmp_size">tmp_table_size (bytes): <span class="value-pill" data-pill-for="tmp_size">{TMP_TABLE_SIZE_DEFAULT}</span></label>
      <input type="range" id="tmp_size" name="tmp_size" min="65536" max="134217728" step="65536" value="{TMP_TABLE_SIZE_DEFAULT}">
      <div class="hint">Default: 16 MiB. MySQL uses min(tmp_table_size, max_heap_table_size).</div>
    </div>

    <div class="control">
      <label for="max_heap">max_heap_table_size (bytes): <span class="value-pill" data-pill-for="max_heap">{MAX_HEAP_TABLE_SIZE_DEFAULT}</span></label>
      <input type="range" id="max_heap" name="max_heap" min="65536" max="134217728" step="65536" value="{MAX_HEAP_TABLE_SIZE_DEFAULT}">
      <div class="hint">Default: 16 MiB. The effective limit is min(tmp_table_size, max_heap_table_size).</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- GROUP BY without index \u2192 internal temp table\n"
            "SELECT   department, COUNT(*) AS headcount\n"
            "FROM     employees\n"
            "GROUP BY department\n"
            "HAVING   COUNT(*) > 5;"
        ),
        note="Internal temp-table operator flow: materialize rows in MEMORY first, then convert to on-disk InnoDB when the effective limit is exceeded."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Rows from child operators are inserted into internal MEMORY temp table first.",
            "Capacity is bounded by min(tmp_table_size, max_heap_table_size).",
            "Once limit is crossed, MySQL converts to on-disk InnoDB temp table.",
            "Remaining inserts hit disk-backed structure, increasing latency.",
            "Higher effective limit delays conversion and reduces disk I/O risk.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="tmp-svg" viewBox="0 0 800 360" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (internal temporary tables)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Effective limit {ht("MySQL uses min(tmp_table_size, max_heap_table_size) as the effective MEMORY temp table limit.")}</p><p class="value" id="out-limit">\u2014</p></div>
    <div class="item"><p class="label">MEMORY capacity (rows) {ht("How many rows fit in the MEMORY temp table before the on-disk conversion is triggered.")}</p><p class="value" id="out-cap">\u2014</p></div>
    <div class="item"><p class="label">Rows in MEMORY {ht("Rows inserted while the temp table is still in-memory. These are fast, no disk I/O.")}</p><p class="value" id="out-mem-rows">\u2014</p></div>
    <div class="item"><p class="label">Rows on disk {ht("Rows inserted after the MEMORY limit was exceeded and the table was converted to on-disk InnoDB.")}</p><p class="value" id="out-disk-rows">\u2014</p></div>
    <div class="item"><p class="label">Disk conversion? {ht("If Yes, the MEMORY temp table exceeded the limit and was converted to on-disk InnoDB — a costly operation.")}</p><p class="value ok" id="out-conversion">\u2014</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — when does MySQL use internal temp tables?</summary>
  <div class="body">
    <p>MySQL creates internal temporary tables for several query patterns:</p>
    <p><strong>GROUP BY / DISTINCT / ORDER BY with GROUP BY</strong> — when
    there's no covering index that produces rows in the grouped/sorted order.
    This is the most common trigger.</p>
    <p><strong>UNION / UNION DISTINCT</strong> — the de-duplication step
    materializes into a temp table. <code>UNION ALL</code> does not need one.</p>
    <p><strong>Derived tables and CTEs</strong> — subqueries in FROM clauses
    and non-recursive CTEs are often materialized.</p>
    <p><strong>Semijoin materialization</strong> — the optimizer may
    materialize the inner side of <code>IN (SELECT ...)</code> into a temp
    table and probe it for each outer row.</p>
    <p>The conversion to on-disk happens when <strong>any</strong> of these
    are true: (1) data exceeds <code>min(tmp_table_size, max_heap_table_size)</code>,
    (2) the table contains BLOB/TEXT columns (can't be stored in MEMORY engine),
    (3) the total row length exceeds MEMORY engine limits.</p>
    <p>In MySQL 8.4, on-disk temp tables use the <strong>TempTable</strong>
    storage engine by default (<code>internal_tmp_mem_storage_engine=TempTable</code>),
    which is more efficient than the old InnoDB-backed path. The TempTable engine
    also has its own memory limit: <code>temptable_max_ram</code> (default 1 GiB).</p>
    <p>Sources: MySQL 8.4 reference manual §10.4.4 "Internal Temporary Table Use";
    MySQL 8.4 reference manual §7.1.8 "Server System Variables".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % (TMP_TABLE_SIZE_DEFAULT, MAX_HEAP_TABLE_SIZE_DEFAULT)

    return _html.render_page(
        lesson_id="tmp",
        title="Temporary tables — MEMORY to on-disk conversion",
        subtitle=(
            "Watch a GROUP BY fill a MEMORY temp table, hit the limit, "
            "and convert to on-disk InnoDB. That cliff is why your query suddenly slows down."
        ),
        version_chip="MySQL 8.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
