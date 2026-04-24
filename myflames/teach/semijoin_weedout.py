"""Lesson: Semijoin Duplicate Weedout.

Shows how MySQL rewrites an IN/EXISTS subquery as a semijoin (inner join)
and then uses a temporary table keyed on the outer-table rowid to remove
duplicate outer rows. Concrete sample data: customers and their orders.

Animation zones:
  Zone 1 (top)    — inner join producing rows with duplicates
  Zone 2 (middle) — temp table with rowid column (inserts succeed/fail)
  Zone 3 (bottom) — deduplicated result set
"""
from . import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render():
    # type: () -> str
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (Semijoin Duplicate Weedout)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="outer_rows">Outer table rows (customers): <span class="value-pill" data-pill-for="outer_rows">1000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="100" max="500000" step="100" value="1000">
      <div class="hint">Number of rows in the outer (driving) table.</div>
    </div>

    <div class="control">
      <label for="inner_matches">Avg inner matches per outer row: <span class="value-pill" data-pill-for="inner_matches">5</span></label>
      <input type="range" id="inner_matches" name="inner_matches" min="1" max="100" step="1" value="5">
      <div class="hint">Average orders per customer matching the WHERE clause. This creates duplicates.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
      <div class="hint">Size of each row in the outer table.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- IN subquery \u2192 semijoin \u2192 duplicate weedout\n"
            "SELECT * FROM customers c\n"
            "WHERE  c.id IN (\n"
            "  SELECT o.customer_id\n"
            "  FROM   orders o\n"
            "  WHERE  o.total > 1000\n"
            ");"
        ),
        note=(
            "MySQL rewrites the IN subquery as an inner join, which may "
            "produce duplicate customer rows. DuplicateWeedout uses a temp "
            "table keyed on c.rowid to remove duplicates."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Phase 1 \u2014 inner join: MySQL rewrites the IN subquery as a "
            "normal inner join between customers and orders. Each customer "
            "with multiple matching orders appears multiple times (e.g. Alice "
            "appears twice because she has two orders > $1000).",
            "Phase 2 \u2014 weedout: for each join result row, MySQL tries to "
            "INSERT the outer-table rowid into a temporary table with a "
            "unique key. If the insert succeeds (\u2713 green), the row is new "
            "\u2014 emit it. If it fails (\u2717 red), the rowid was already seen "
            "\u2014 discard the duplicate.",
            "The temp table is keyed on the outer-table rowid (8 bytes). If "
            "unique_rows \u00d7 8 fits in tmp_table_size, the temp table stays "
            "in memory. Otherwise it spills to disk.",
            "The duplicate counter on the right tracks how many rows were "
            "discarded. The deduplicated result set at the bottom shows only "
            "the unique customers that survived.",
            "Total work = join_rows = outer_rows \u00d7 inner_matches. Every "
            "row must be checked against the temp table. Higher fan-out "
            "means more wasted work on duplicates.",
        ],
    )

    stage_html = (
        '<section class="stage">\n'
        "  %s\n"
        "  %s\n"
        "  %s\n"
        '  <div class="stage-with-phases">\n'
        '    <svg id="weedout-svg" viewBox="0 0 800 440"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "    %s\n"
        "  </div>\n"
        "</section>"
    ) % (
        query_card_html,
        explainer_html,
        _html.stage_toolbar("Ready \u2014 press Play"),
        _html.phase_nav(),
    )

    ht = _html.help_tip
    readout_html = (
        '<section class="readout">\n'
        "  <h2>Cost readout (Semijoin Duplicate Weedout)</h2>\n"
        '  <div class="readout-grid">\n'
        '    <div class="item"><p class="label">Join rows (before dedup) '
        + ht("Total rows produced by the inner join = outer_rows \u00d7 inner_matches. All of these must be checked against the temp table.")
        + '</p><p class="value" id="out-join-rows">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Unique rows (after dedup) '
        + ht("At most outer_rows survive after weedout. Each unique outer rowid appears exactly once in the result.")
        + '</p><p class="value" id="out-unique-rows">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Duplicates discarded '
        + ht("join_rows \u2212 unique_rows. These rows were produced by the inner join but rejected because their outer rowid was already in the temp table.")
        + '</p><p class="value" id="out-discarded">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Temp table inserts '
        + ht("Every join row triggers an INSERT attempt into the weedout temp table. Successful inserts mean new row; failed inserts mean duplicate.")
        + '</p><p class="value" id="out-temp-inserts">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Duplication factor '
        + ht("inner_matches \u2014 how many times each outer row is duplicated on average. Higher = more wasted work.")
        + '</p><p class="value" id="out-dup-factor">\u2014</p></div>\n'
        '    <div class="item"><p class="label">Temp table location '
        + ht("If unique_rows \u00d7 8 bytes fits in tmp_table_size (16 MiB default), the temp table stays in memory. Otherwise it spills to disk.")
        + '</p><p class="value ok" id="out-temp-memory">\u2014</p></div>\n'
        "  </div>\n"
        '  <div class="explanation" id="out-explanation"></div>\n'
        '  <div class="complexity-chart">\n'
        '    <p class="chart-title">Weedout work vs materialization (log\u2013log)</p>\n'
        '    <svg id="complexity-chart" viewBox="0 0 560 200"'
        ' xmlns="http://www.w3.org/2000/svg"></svg>\n'
        "  </div>\n"
        "</section>"
    )

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more \u2014 DuplicateWeedout among the four semijoin strategies</summary>
  <div class="body">
    <p>MySQL\u2019s optimizer can rewrite <code>IN (SELECT \u2026)</code> and
    <code>EXISTS (SELECT \u2026)</code> subqueries as semijoins. It then
    chooses among four execution strategies:</p>

    <p><strong>1. FirstMatch</strong> \u2014 as soon as the first inner row
    matches an outer row, stop scanning the inner table for that outer row.
    Works well when the subquery is correlated and selective.</p>

    <p><strong>2. LooseScan</strong> \u2014 scans the inner table\u2019s index
    and skips duplicate key values, feeding only distinct keys to the outer
    join. Requires a suitable index on the inner side.</p>

    <p><strong>3. DuplicateWeedout</strong> (this lesson) \u2014 runs the
    full inner join, then removes duplicates using a temporary table keyed
    on the outer-table rowid. The most general strategy \u2014 works even
    when FirstMatch and LooseScan can\u2019t.</p>

    <p><strong>4. Materialization</strong> \u2014 materializes the subquery
    into a temp table once, then probes it for each outer row. Good when
    the subquery result is small and reusable.</p>

    <p>DuplicateWeedout is controlled by
    <code>optimizer_switch=duplicateweedout=on</code> (enabled by default).
    Its cost depends on the join fan-out (how many inner rows match each
    outer row) and whether the weedout temp table fits in memory.</p>

    <p>When the fan-out is high, DuplicateWeedout does significant wasted
    work processing duplicate rows. In those cases, Materialization or
    FirstMatch may be cheaper \u2014 but DuplicateWeedout is the fallback
    that always works.</p>

    <p>Sources: MySQL 8.4 Reference Manual \u00a78.2.2.1 \u201cOptimizing
    IN and EXISTS Subquery Predicates with Semijoin Transformations\u201d;
    MariaDB Knowledge Base \u201cSemijoin Subquery Optimizations\u201d.</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="semijoin_weedout",
        title="Semijoin Duplicate Weedout \u2014 dedup via temp table",
        subtitle=(
            "MySQL rewrites IN/EXISTS subqueries as inner joins, then uses "
            "a temporary table keyed on the outer rowid to remove duplicates. "
            "Watch the weedout process row by row."
        ),
        version_chip="MySQL 8.4 \u2022 MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
