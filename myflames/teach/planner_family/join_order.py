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

# Guided steps (see _html.normalize_guide). join_order.js marks the problem,
# each costing round, the prune, each "go deeper" and the result (and the
# hypergraph phases) with tl.beat(kind). Checked in MySQL sql/sql_planner.cc
# (greedy_search, best_extension_by_limited_search, determine_search_depth)
# and MariaDB sql/sql_select.cc (the same three): the search extends partial
# orders depth first up to search_depth tables, and with optimizer_prune_level
# set drops a candidate when an earlier one at the same step had no more rows
# and no higher cost. The MySQL function names are on
# _html.VERIFIED_SERVER_FUNCTIONS (checked in mysql-server 8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Join order search (greedy_search)",
        "lines": [
            ("start", 0, "plan = empty; all N tables remain"),
            ("loop", 0, "while tables remain (search search_depth ahead):",
             "Optimize_table_order::greedy_search"),
            ("each", 1, "for each table t that can go next:",
             "Optimize_table_order::best_extension_by_limited_search"),
            ("cost", 2, "cost plan + t: access path, rows, cost",
             "Optimize_table_order::best_access_path"),
            ("heur", 2, "if prune_level and an earlier t had <= rows and cost:"),
            ("drop", 3, "drop plan + t and every order after it"),
            ("deeper", 2, "else repeat for the tables after plan + t"),
            ("pick", 1, "plan += cheapest order's first table (or all)"),
            ("done", 0, "return plan: the order EXPLAIN shows"),
            ("hg", 0, "hypergraph (MySQL only): a plan per table set"),
            ("hg-one", 1, "each table alone: its cheapest access path"),
            ("hg-pairs", 1, "for two connected sets a predicate links:"),
            ("hg-join", 2, "keep the cheapest plans for their union"),
            ("hg-done", 1, "return the plan for all tables (may be bushy)"),
        ],
    },
    "beats": {
        "problem": ["start"],
        "cost-first": ["loop", "each", "cost"],
        "go-deeper": ["deeper"],
        "prune": ["heur", "drop"],
        "cost-next": ["each", "cost"],
        "done": ["pick", "done"],
        "hg-tables": ["hg", "hg-one"],
        "hg-pairs": ["hg-pairs", "hg-join"],
        "hg-grow": ["hg-pairs", "hg-join"],
        "hg-done": ["hg-done"],
    },
    "quiz": [
        {
            "question": ("Seven tables, optimizer_search_depth at its default of 62. How many "
                         "left-deep join orders can greedy_search consider?"),
            "choices": [
                ("All 7! = 5,040, because the search reaches every table", True,
                 "When search_depth is at least the number of tables the search can cover "
                 "every order, which the server source describes as factorial in the number "
                 "of tables."),
                ("7, one per table", False,
                 "That would be a single greedy step; a depth of 62 looks ahead through all "
                 "7 tables."),
                ("Only the order written in FROM", False,
                 "MySQL and MariaDB both reorder inner joins by estimated cost unless you force "
                 "the order with STRAIGHT_JOIN."),
            ],
        },
        {
            "question": "What does optimizer_prune_level=1 do during the search?",
            "choices": [
                ("Drops a partial order when an earlier candidate at the same step read no more "
                 "rows and cost no more, without exploring its extensions", True,
                 "This heuristic shrinks the search a lot but can miss the cheapest order; "
                 "MySQL's level 1 also adds a chain of eq_ref joins as one group, which "
                 "MariaDB does at its default level 2."),
                ("Stops planning after 1 millisecond", False,
                 "The value is a level, not a time; it turns on a rule about which partial "
                 "orders are worth exploring."),
                ("Guarantees the cheapest order", False,
                 "It is a heuristic, and the server source warns that it may miss the optimal "
                 "plan; 0 turns it off."),
            ],
        },
        {
            "question": ("A 20-table report spends seconds choosing its join order. Which change "
                         "trades plan quality for a faster choice?"),
            "choices": [
                ("A lower optimizer_search_depth, such as 0, which picks 7 for this query", True,
                 "A smaller depth looks fewer tables ahead before committing one, and 0 makes "
                 "both servers use 7 when more than 7 tables are left to order."),
                ("optimizer_prune_level=0", False,
                 "That turns the pruning heuristic off, so the search explores more orders and "
                 "takes longer."),
                ("A larger join_buffer_size", False,
                 "The join buffer holds rows while the query runs; its size does not shorten "
                 "the search for a join order."),
            ],
        },
    ],
}


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
        <option value="1" selected>1: prune by rows and cost (MySQL default)</option>
        <option value="0">0: no pruning heuristic</option>
      </select>
      <div class="hint">MariaDB defaults to 2. MySQL at 1 and MariaDB at 2 also add a chain of eq_ref joins as one group. Set 0 only when debugging; pruning is what keeps planning fast.</div>
    </div>

    <div class="control">
      <label for="algo">Search algorithm</label>
      <select id="algo" name="algo">
        <option value="greedy" selected>greedy_search (default optimizer)</option>
        <option value="hypergraph">hypergraph optimizer (MySQL only, off by default)</option>
      </select>
      <div class="hint">Hypergraph (DPhyp) is set-based with an <code>optimizer_max_subgraph_pairs</code> cap. MySQL builds before 9.7 include it only in debug builds by default; MariaDB has none.</div>
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
            "Top: the N tables (u = users, o = orders, li = line_items, p = products, s = suppliers, c = categories, w = warehouses) as candidate roots. Every search starts by picking one of them as the outermost loop.",
            "Center: the search tree of partial plans, three levels of it. Each node is a partial join order the planner costed; the path from the top to a node is that order.",
            "Orange pulses = a partial plan is being costed by best_extension_by_limited_search. Green = the cheapest order so far, which the search extends.",
            "Grey, dashed branches = optimizer_prune_level=1 dropped them: an earlier candidate at the same step read no more rows and cost no more, so their extensions are never explored. With 0 every drawn branch is costed.",
            "Bottom strip: the chosen final order, what EXPLAIN would print. Counter (top right): how many partial plans were costed; watch it explode when you raise search_depth and turn pruning off.",
            "Next step plays one stage of the search at a time; the simplified code beside the drawing highlights the lines each step runs.",
        ],
    )

    stage_html = f"""
<section class="stage" aria-labelledby="stage-h">
  <h2 id="stage-h" class="sr-only" style="position:absolute;left:-9999px">Join-order search animation</h2>
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
  <div class="stage-with-phases">
    <svg id="planner-svg" viewBox="0 0 820 436" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (updates live)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">N (tables) {ht("Tables the search must order. const tables are placed first and do not count, and with MySQL's prune_level 1 or MariaDB's 2 a chain of eq_ref joins is added as one group, so the source counts non-eq_ref tables plus eq_ref groups.")}</p><p class="value" id="out-n">—</p></div>
    <div class="item"><p class="label">effective_search_depth {ht("min(optimizer_search_depth, N). When equal to N the search reduces to full exhaustive enumeration.")}</p><p class="value" id="out-depth">—</p></div>
    <div class="item"><p class="label">Plans evaluated {ht("Concrete count for this N and depth. The number of times best_extension_by_limited_search is called.")}</p><p class="value" id="out-plans">—</p></div>
    <div class="item"><p class="label">Worst-case complexity {ht("From the comment above greedy_search (sql_planner.cc in MySQL, sql_select.cc in MariaDB): when search_depth >= N, greedy_search is O(N!). For smaller depth d: O(N * N^d / d).")}</p><p class="value" id="out-complexity">—</p></div>
    <div class="item"><p class="label">Pruning ratio {ht("Fraction of partial plans that optimizer_prune_level=1 dropped before evaluating their full subtree. 0% means pruning is off. This lesson assumes 75%; the real share depends on the query and its statistics.")}</p><p class="value" id="out-prune">—</p></div>
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
    <pre style="background:var(--surface-subtle);color:var(--ink);padding:10px;border-radius:6px;font-size:11.5px;line-height:1.5;overflow-x:auto">
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
    considered; the only orders cut short are those already costlier
    than the best complete order found so far, a bound both servers
    always apply. With a depth limit <code>d</code>, only the next
    <code>d</code> tables are enumerated exhaustively before the search
    commits to a partial prefix, so the bound drops to
    O(N · N<sup>d</sup> / d).</p>

    <p><strong>Defaults you might tune (MySQL 8.4 / MariaDB 11.4):</strong></p>
    <ul>
      <li><code>optimizer_search_depth</code> = <strong>62</strong>
      (MAX_TABLES + 1) in both. Setting it to <code>0</code> tells the
      server to pick a value automatically: the number of tables plus one
      for up to 7 tables, otherwise 7. Session-tunable, so you can drop it
      per-query if planning is a bottleneck.</li>
      <li><code>optimizer_prune_level</code> = <strong>1</strong> on MySQL
      (0 or 1): skip a partial order that reads no fewer rows and costs no
      less than one already costed at the same step, and add a chain of
      eq_ref joins as one group. MariaDB accepts 0 to 2 and defaults to
      <strong>2</strong>, the level at which MariaDB groups eq_ref chains. Set <code>0</code> only when you need to compare what the
      planner would have chosen without heuristics, never in production.</li>
      <li><code>optimizer_max_subgraph_pairs</code> = <strong>100000</strong>.
      Only MySQL's hypergraph (DPhyp) optimizer respects this. MySQL
      builds before 9.7 include that optimizer only in debug builds by
      default; from 9.7 the build includes it by default, and it stays off
      until
      <code>SET optimizer_switch='hypergraph_optimizer=on'</code>.
      MariaDB has no hypergraph optimizer.</li>
    </ul>

    <p><strong>N is not always the table count in your FROM clause.</strong>
    The source comment is precise: <em>"N is the number of non-eq_ref
    tables + eq_ref groups, which normally are considerably less than
    total numbers of tables in the query."</em> <code>const</code>
    tables are placed before <code>greedy_search</code> runs, and with
    MySQL's <code>optimizer_prune_level=1</code> or MariaDB's
    <code>2</code> a chain of <code>eq_ref</code> joins is added as one
    group during the search, so a 10-table join can behave like N=4 (three
    tables plus one eq_ref group) if the other seven tables form one chain
    of primary-key equi-joins.</p>

    <p><strong>How is this different from the join algorithms?</strong>
    Hash join, BNL, nested loop are <em>execution</em> strategies — they
    decide how a single join *runs* once the planner has fixed its place
    in the tree. <code>greedy_search</code> is the layer above: given a
    multi-way join, which pair joins first? Which result becomes the
    outer side of the next join? EXPLAIN's table-order column is the
    output of this search.</p>

    <p>Sources: MySQL 8.4 Reference Manual §10.9.3 (Controlling
    Switchable Optimizations); <code>sql/sql_planner.cc</code>
    (<code>greedy_search</code>, <code>best_extension_by_limited_search</code>)
    in mysql-server 8.4 and 9.7; <code>sql/sql_select.cc</code> (the same
    functions) and <code>sql/sys_vars.cc</code> in MariaDB. MariaDB
    Knowledge Base "Optimizer Switch".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE

    return _html.render_page(
        lesson_id="join_order",
        title="Join-order search: why query planning is factorial",
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
        guide=GUIDE,
    )
