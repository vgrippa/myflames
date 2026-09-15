"""Lesson: Semijoin FirstMatch — the early-out on the first matching row.

The flagship of the semijoin cluster. A ``WHERE col IN (subquery)`` or
``EXISTS (subquery)`` is a *does-it-exist* question, not a *how-many*
question. The optimizer rewrites it as a semijoin and, with the
FirstMatch strategy, stops scanning the inner side the instant one
matching row is found for the current outer row — the inner scan
short-circuits.

Two acts drive the punchline (agreed with the teaching skill owner):

* **Act 1 — the naive mental model**: a plain inner join scans *every*
  matching order for each customer. If Alice has three orders over
  $1000, all three are read. That is wasted work for ``IN``.
* **Act 2 — FirstMatch early-out**: the same query, executed as a
  semijoin. The moment the *first* qualifying order is found for a
  customer, MySQL emits that customer and skips straight to the next
  customer. The later orders are never read — greyed out in the
  animation. The counters diverge: rows-read-naive climbs, rows-read
  FirstMatch flattens.

Structural template: ``nested_loop.py`` (the modern flagship pattern —
``_html.load_lesson_js`` sibling ``.js``, f-string HTML, ``anim.path``
arc pills, phase-nav, tweened arrivals, interactive complexity chart).

FirstMatch source note (route to mysql-correctness-reviewer):
the strategy lives in ``sql/sql_executor.cc`` /
``sql/iterators/composite_iterators`` as the ``FirstMatch`` semijoin
strategy; enabled by ``optimizer_switch='firstmatch=on'`` (default on)
in MySQL 8.4 and MariaDB 11.4. The claim animated here is behavioural
("scanning of the inner table for the current outer row stops after the
first match"), which should be checked against source before shipping
any hard number.
"""
from .. import _html


_LESSON_JS = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = """
<section class="controls">
  <h2>Semijoin shape controls</h2>
  <div class="control-grid">
    <div class="control">
      <label for="outer_rows">Outer rows (customers): <span class="value-pill" data-pill-for="outer_rows">50000</span></label>
      <input type="range" id="outer_rows" name="outer_rows" min="1000" max="5000000" step="1000" value="50000">
      <div class="hint">Rows in the driving table — the customers the IN subquery is checked against.</div>
    </div>
    <div class="control">
      <label for="inner_matches">Matching inner rows per outer row (orders &gt; $1000): <span class="value-pill" data-pill-for="inner_matches">8</span></label>
      <input type="range" id="inner_matches" name="inner_matches" min="1" max="200" step="1" value="8">
      <div class="hint">Average qualifying orders per customer. A plain join reads them all; FirstMatch reads only until the first one.</div>
    </div>
  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "SELECT c.customer_id, c.name\n"
            "FROM   customers c\n"
            "WHERE  c.customer_id IN (\n"
            "  SELECT o.customer_id\n"
            "  FROM   orders o\n"
            "  WHERE  o.total > 1000\n"
            ");"
        ),
        note=(
            "IN (subquery) asks does-it-exist, not how-many. MySQL rewrites "
            "it as a semijoin and runs the FirstMatch strategy: the moment "
            "the first order over $1000 is found for a customer, that "
            "customer is emitted and the inner scan short-circuits — the "
            "remaining orders for that customer are never read."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Left = the driving customers (outer rows). Orange = the "
            "customer currently being checked. Right = that customer's "
            "orders over $1000 (the inner rows the subquery scans).",
            "Act 1 — naive inner join: for each customer, EVERY matching "
            "order arcs into the probe panel. Alice with 3 orders reads "
            "all 3. The 'rows read' counter climbs fast — most of that "
            "work is wasted for an IN check.",
            "Act 2 — FirstMatch early-out: the same query as a semijoin. "
            "The FIRST matching order arcs in, the customer is emitted "
            "with a green check, and the inner scan STOPS. The skipped "
            "orders are drawn greyed-out — MySQL never reads them.",
            "The two 'rows read' counters diverge — that divergence IS "
            "the lesson. Naive = outer_rows × matches; FirstMatch = "
            "outer_rows × 1 (one probe stops each inner scan).",
            "Watch the phase label: it names the exact customer and order "
            "at every step ('Alice id=1 -> order #104 $1500 matches -> "
            "emit Alice, stop scanning her orders').",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="fm-svg" viewBox="0 0 800 420" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>FirstMatch cost model</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Outer rows {ht("Rows from the driving (customers) side. The semijoin runs the existence check once per outer row.")}</p><p class="value" id="out-outer">—</p></div>
    <div class="item"><p class="label">Matching inner rows each {ht("Average orders over $1000 per customer. A plain join reads all of them; FirstMatch reads only until the first.")}</p><p class="value" id="out-inner">—</p></div>
    <div class="item"><p class="label">Rows read — naive join {ht("Inner rows a plain inner join would read: outer_rows × matches. All of it is wasted past the first match, because IN only needs existence.")}</p><p class="value hot" id="out-naive">—</p></div>
    <div class="item"><p class="label">Rows read — FirstMatch {ht("Inner rows FirstMatch reads: about outer_rows × 1. The inner scan short-circuits on the first match for each outer row.")}</p><p class="value ok" id="out-firstmatch">—</p></div>
    <div class="item"><p class="label">Inner reads saved {ht("naive − FirstMatch. These are inner-row reads FirstMatch skips because IN asks does-it-exist, not how-many.")}</p><p class="value ok" id="out-saved">—</p></div>
    <div class="item"><p class="label">Complexity {ht("FirstMatch turns the inner cost per outer row from 'read all matches' into 'read until the first match' — roughly constant per outer row.")}</p><p class="value">O(n · 1)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Inner rows read: naive join vs FirstMatch (log-log)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — FirstMatch among the four semijoin strategies</summary>
  <div class="body">
    <p>MySQL and MariaDB rewrite <code>IN (SELECT …)</code> and
    <code>EXISTS (SELECT …)</code> subqueries as <strong>semijoins</strong>
    — a join that keeps an outer row if <em>at least one</em> inner row
    matches, without duplicating the outer row for every match. That
    "at least one" is the whole point: the subquery asks
    <em>does a matching row exist?</em>, never <em>how many?</em>.</p>

    <p><strong>FirstMatch</strong> (this lesson) exploits exactly that.
    While scanning the inner side for the current outer row, the instant
    the first qualifying inner row is found, the outer row is emitted and
    the inner scan for that outer row <em>stops</em>. No further inner
    rows are read for that customer. It behaves like a nested loop with
    an early <code>break</code> the moment a match appears — which is why
    it is the cheapest strategy when each outer row has many matches you
    would otherwise read for nothing.</p>

    <p>The other three strategies handle the cases FirstMatch can't:</p>
    <ul>
      <li><strong>LooseScan</strong> — walks the inner index and skips
      duplicate key values, feeding only distinct keys to the join.
      Needs a suitable index on the inner side.</li>
      <li><strong>Materialization</strong> — runs the subquery once into
      a temp table, then probes it per outer row. Good when the subquery
      result is small and reusable.</li>
      <li><strong>DuplicateWeedout</strong> — runs the full join, then
      removes duplicate outer rows via a temp table keyed on the outer
      rowid. The most general fallback — it always works.</li>
    </ul>

    <p>FirstMatch is controlled by
    <code>optimizer_switch='firstmatch=on'</code> (enabled by default in
    MySQL 8.4 and MariaDB 11.4). The optimizer picks it when the outer
    row's existence check can be satisfied by the first inner match and
    reading the rest would be wasted work.</p>

    <p>Practical takeaway: if <code>EXPLAIN</code> shows
    <code>FirstMatch(customers)</code> (or the JSON tree shows a semijoin
    with a first-match strategy), MySQL is <em>already</em> short-circuiting
    the inner scan for you. If instead you see the full inner side being
    read per outer row, an index on the inner join/filter column is what
    lets the optimizer keep the per-outer cost near 1.</p>

    <p>Sources: MySQL 8.4 Reference Manual §10.2.2.1 "Optimizing IN and
    EXISTS Subquery Predicates with Semijoin Transformations"; MariaDB
    Knowledge Base "Semijoin Subquery Optimizations" / "FirstMatch
    Strategy". Behavioural claim (inner scan stops on first match) should
    be verified against the FirstMatch semijoin strategy in
    <code>sql/</code> before shipping any hard cost number.</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="semijoin_firstmatch",
        title="Semijoin FirstMatch — stop looking after the first match",
        subtitle=(
            "IN (subquery) asks does-it-exist, not how-many. Watch MySQL "
            "short-circuit the inner scan the moment one matching order "
            "appears — the naive-join and FirstMatch counters diverge."
        ),
        version_chip="MySQL 8.4 • MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS,
    )
