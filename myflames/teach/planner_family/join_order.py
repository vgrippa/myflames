"""Lesson: join-order search and why it is factorial in the worst case.

Animates ``Optimize_table_order::greedy_search`` (sql/sql_planner.cc):
a forward, depth-limited enumeration that extends a partial plan one
table at a time. The search tree of partial plans is drawn live so the
viewer can watch:

* search_depth = 1  → pure greedy   → O(N) plans evaluated
* search_depth = 2  → 2-step lookahead → O(N · N)
* search_depth = N  → exhaustive    → O(N!) — verified by the source
  comment: "When search_depth >= N, then the complexity of greedy_search
  is O(N!)" — sql_planner.cc line 2311

The lesson also toggles ``optimizer_prune_level`` (heuristic cost-based
pruning, default 1) so the user can see partial plans grey out before
their subtree is explored.

All cost-model claims here are version-gated to MySQL 8.4 / MariaDB 11.4
and traced to the planner source. See the Learn More section.
"""
from __future__ import annotations

from .. import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters</h2>
  <div class="control-grid">

    <div class="control">
      <label for="tables">Tables in query (N): <span class="value-pill" data-pill-for="tables">7</span></label>
      <input type="range" id="tables" name="tables" min="2" max="10" step="1" value="7">
      <div class="hint">Real OLTP joins rarely exceed 6 tables; warehouse queries can hit 15+.</div>
    </div>

    <div class="control">
      <label for="search_depth">optimizer_search_depth: <span class="value-pill" data-pill-for="search_depth">62</span></label>
      <input type="range" id="search_depth" name="search_depth" min="1" max="10" step="1" value="10">
      <div class="hint">Default is MAX_TABLES+1 = 62 (effective exhaustive). 1 = pure greedy.</div>
    </div>

    <div class="control">
      <label for="prune_level">optimizer_prune_level</label>
      <select id="prune_level" name="prune_level">
        <option value="1" selected>1 — cost-based pruning (default)</option>
        <option value="0">0 — no pruning, exhaustive</option>
      </select>
      <div class="hint">Set 0 only when debugging; pruning is what keeps planning sub-millisecond.</div>
    </div>

    <div class="control">
      <label for="algo">Search algorithm</label>
      <select id="algo" name="algo">
        <option value="greedy" selected>greedy_search (default optimizer)</option>
        <option value="hypergraph">hypergraph optimizer (MySQL 8.0+)</option>
      </select>
      <div class="hint">Hypergraph (DPhyp) is set-based with <code>optimizer_max_subgraph_pairs</code> cap.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Seven-table join: the planner must pick an order before execution\n"
            "SELECT u.name, SUM(li.qty * li.unit_price) AS revenue\n"
            "FROM   users      u\n"
            "JOIN   orders     o  ON o.user_id     = u.id\n"
            "JOIN   line_items li ON li.order_id   = o.id\n"
            "JOIN   products   p  ON p.id          = li.product_id\n"
            "JOIN   suppliers  s  ON s.id          = p.supplier_id\n"
            "JOIN   categories c  ON c.id          = p.category_id\n"
            "JOIN   warehouses w  ON w.id          = li.warehouse_id\n"
            "WHERE  u.country = 'US' AND s.region = 'EU'\n"
            "GROUP BY u.name;"
        ),
        note=(
            "7! = 5,040 possible left-deep orders. EXPLAIN shows the one MySQL "
            "picked; this lesson shows the search that picked it."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Top: the N tables as candidate roots — every search starts by picking one of them as the outermost loop.",
            "Center: the search tree of partial plans. Each node is a (partial-order, cost) pair the planner evaluated.",
            "Yellow pulses = a partial plan is being costed by best_extension_by_limited_search.",
            "Grey-out = optimizer_prune_level=1 dropped this branch because its partial cost already exceeded the best complete plan found so far.",
            "Bottom strip: the chosen final order — what EXPLAIN would print.",
            "Counter (top-right of stage): how many partial plans were actually evaluated. Watch it explode when you raise search_depth and turn pruning off.",
        ],
    )

    stage_html = f"""
<section class="stage" aria-labelledby="stage-h">
  <h2 id="stage-h" class="sr-only" style="position:absolute;left:-9999px">Join-order search animation</h2>
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Adjust parameters, then press Play")}
  <div class="stage-with-phases">
    <svg id="planner-svg" viewBox="0 0 820 460" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (updates live)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">N (tables) {ht("Number of non-eq_ref tables plus eq_ref groups. const and eq_ref tables are pre-placed by the planner and do not enter greedy_search.")}</p><p class="value" id="out-n">—</p></div>
    <div class="item"><p class="label">effective_search_depth {ht("min(optimizer_search_depth, N). When equal to N the search reduces to full exhaustive enumeration.")}</p><p class="value" id="out-depth">—</p></div>
    <div class="item"><p class="label">Plans evaluated {ht("Concrete count for this N and depth. The number of times best_extension_by_limited_search is called.")}</p><p class="value" id="out-plans">—</p></div>
    <div class="item"><p class="label">Worst-case complexity {ht("From sql_planner.cc line 2311: when search_depth >= N, greedy_search is O(N!). For smaller depth d: O(N * N^d / d).")}</p><p class="value" id="out-complexity">—</p></div>
    <div class="item"><p class="label">Pruning ratio {ht("Fraction of partial plans that optimizer_prune_level=1 dropped before evaluating their full subtree. 0% means pruning is off; real OLTP plans see 60-90%.")}</p><p class="value" id="out-prune">—</p></div>
    <div class="item"><p class="label">Planning time estimate {ht("At ~1 microsecond per partial plan on modern hardware. Compare to query execution: if planning approaches execution cost, raise optimizer_prune_level or lower optimizer_search_depth.")}</p><p class="value" id="out-time">—</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Plans evaluated vs N (log–log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — what the source actually says about greedy_search</summary>
  <div class="body">
    <p>The algorithm is <code>Optimize_table_order::greedy_search</code>
    in <code>sql/sql_planner.cc</code>. The doxygen comment above its
    body documents the complexity directly:</p>
    <pre style="background:#0f172a;color:#e2e8f0;padding:10px;border-radius:6px;font-size:11.5px;line-height:1.5;overflow-x:auto">
procedure greedy_search
  input: remaining_tables
  output: pplan;
{
  pplan = &lt;&gt;;
  do {
    (t, a) = best_extension(pplan, remaining_tables);
    pplan = concat(pplan, (t, a));
    remaining_tables = remaining_tables - t;
  } while (remaining_tables != {})
  return pplan;
}

"the worst-case complexity of this algorithm is
 &lt;= O(N * N^search_depth / search_depth).
 When search_depth &gt;= N, then the complexity
 of greedy_search is O(N!)."</pre>

    <p><strong>Why N!, intuitively?</strong> A left-deep plan over N
    tables is an ordering — there are N! orderings. With
    <code>search_depth ≥ N</code> and pruning off, every ordering is
    costed. With a depth limit <code>d</code>, only the next
    <code>d</code> tables are enumerated exhaustively before the search
    commits to a partial prefix, so the bound drops to
    O(N · N<sup>d</sup> / d).</p>

    <p><strong>Defaults you might tune (MySQL 8.4 / MariaDB 11.4):</strong></p>
    <ul>
      <li><code>optimizer_search_depth</code> = <strong>62</strong>
      (MAX_TABLES + 1). Setting it to <code>0</code> tells the server to
      pick a value automatically. Session-tunable, so you can drop it
      per-query if planning is a bottleneck.</li>
      <li><code>optimizer_prune_level</code> = <strong>1</strong>
      (cost-based pruning enabled). Set <code>0</code> only when you
      need to compare what the planner would have chosen without
      heuristics — never in production.</li>
      <li><code>optimizer_max_subgraph_pairs</code> = <strong>100000</strong>.
      Only the hypergraph (DPhyp) optimizer respects this; it's the
      modern set-based optimizer enabled per-query via
      <code>SET optimizer_switch='hypergraph_optimizer=on'</code> on
      MySQL 8.0+.</li>
    </ul>

    <p><strong>N is not always the table count in your FROM clause.</strong>
    The source comment is precise: <em>"N is the number of non-eq_ref
    tables + eq_ref groups, which normally are considerably less than
    total numbers of tables in the query."</em> <code>const</code> and
    <code>eq_ref</code> chains are placed before <code>greedy_search</code>
    runs, so a 10-table join can have N=3 if seven of the joins are
    primary-key equi-joins.</p>

    <p><strong>How is this different from the join algorithms?</strong>
    Hash join, BNL, nested loop are <em>execution</em> strategies — they
    decide how a single join *runs* once the planner has fixed its place
    in the tree. <code>greedy_search</code> is the layer above: given a
    multi-way join, which pair joins first? Which result becomes the
    outer side of the next join? EXPLAIN's table-order column is the
    output of this search.</p>

    <p>Sources: MySQL 8.4 Reference Manual §10.9.3 (Controlling
    Switchable Optimizations); <code>sql/sql_planner.cc</code> lines
    2275-2334 in mysql-server master. MariaDB Knowledge Base
    "Optimizer Switch".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="join_order",
        title="Join-order search — why query planning is factorial",
        subtitle=(
            "The greedy_search algorithm enumerates partial plans up to "
            "optimizer_search_depth tables ahead, costed by the optimizer, "
            "pruned by optimizer_prune_level."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
