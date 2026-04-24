"""Lesson: Filter operator (WHERE predicate).

Explains what a plan-stage filter means: rows arrive from the child operator,
the predicate is evaluated row-by-row, and only matching rows flow forward.
"""
from .. import _html


_LESSON_JS = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (filter stage)</h2>
  <div class="control-grid">
    <div class="control">
      <label for="input_rows">Input rows</label>
      <input type="range" id="input_rows" name="input_rows" min="1000" max="10000000" step="1000" value="1000000">
      <div class="hint">Rows entering filter: <span data-pill-for="input_rows">1000000</span></div>
    </div>
    <div class="control">
      <label for="selectivity">Predicate selectivity (%)</label>
      <input type="range" id="selectivity" name="selectivity" min="0.1" max="100" step="0.1" value="5.0">
      <div class="hint">Rows kept by WHERE: <span data-pill-for="selectivity">5.0</span>%</div>
    </div>
  </div>
</section>
"""
    query_card_html = _html.query_card(
        "SELECT order_id, total FROM orders WHERE total > 500;",
        "EXPLAIN Filter node behavior: child operator emits rows, then this stage evaluates WHERE total > 500 row-by-row before passing rows upward.",
    )
    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Left stream is child output rows entering the filter stage.",
            "Middle Filter operator evaluates predicate logic for every incoming row.",
            "Green rows satisfy the predicate and continue to parent operators.",
            "Red rows are discarded, but CPU work already happened for them.",
        ],
    )
    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="filter-svg" viewBox="0 0 800 390" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""
    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (filter operator)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Input rows {ht("Rows entering the filter stage from its child operator.")}</p><p class="value" id="out-in">—</p></div>
    <div class="item"><p class="label">Output rows {ht("Rows that satisfy the WHERE predicate and continue upward in the plan.")}</p><p class="value" id="out-out">—</p></div>
    <div class="item"><p class="label">Dropped rows {ht("Rows evaluated but rejected by the predicate.")}</p><p class="value" id="out-drop">—</p></div>
    <div class="item"><p class="label">Selectivity {ht("Percentage of input rows that survive the predicate.")}</p><p class="value" id="out-sel">—</p></div>
  </div>
  <div class="explanation" id="out-exp"></div>
  <div class="complexity-chart">
    <p class="chart-title">Rows evaluated vs rows returned (log–log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""
    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — why filter nodes still matter</summary>
  <div class="body">
    <p>A Filter node is not always bad — it is normal in many plans. The key question is how many rows arrive at this stage.</p>
    <p>If a selective predicate is pushed down into an index access path, far fewer rows reach this filter stage.</p>
    <p>When you see large input rows and tiny output rows, consider adding/adjusting indexes so filtering happens earlier.</p>
  </div>
</details>
"""
    return _html.render_page(
        lesson_id="filter",
        title="Filter operator — WHERE predicate row-by-row",
        subtitle="Understand the internal execution loop of a Filter stage: evaluate every input row, then keep or discard.",
        version_chip="MySQL 8.4 • MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS,
    )

