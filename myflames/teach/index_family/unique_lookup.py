"""Lesson: Unique Key Lookup (single-row index lookup).

Shares the visual vocabulary of the flagship ``btree`` lesson: a real
B+tree (root / internal / leaf pages) with a red diamond query token
descending page by page, yellow page-pulses on arrival, and a dashed
orange PK link when the lookup is non-covering, followed by a second walk
of the clustered index from its root page (the secondary record holds the
primary key, not a page address). The UNIQUE special case is called out
by a small chip: exactly one leaf record can match, not a range.
"""
from .. import _html
from .._cost_model import (
    INNODB_PAGE_OVERHEAD_BYTES,
    INNODB_CHILD_POINTER_BYTES,
    INNODB_PAGE_SIZE_DEFAULT,
)


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). unique_lookup.js marks one step
# per page read, the UNIQUE stop, the PK hop and the end with tl.beat(kind),
# as the family flagship btree does. Function names are on
# _html.VERIFIED_SERVER_FUNCTIONS (checked in mysql-server 8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Unique index lookup in InnoDB",
        "lines": [
            ("root", 0, "page = root page of the index", "btr_cur_search_to_nth_level"),
            ("loop", 0, "while page is not a leaf page:"),
            ("child", 1, "page = child page whose key range holds the key",
             "page_cur_search_with_match"),
            ("rec", 0, "rec = record in the leaf page with the key"),
            ("unique", 0, "UNIQUE index: no second record can match, so stop",
             "read_const"),
            ("covers", 0, "if the query needs a column rec lacks (first_name):"),
            ("pk", 1, "key = rec.id, the primary key stored in the record",
             "row_build_row_ref_in_tuple"),
            ("clus", 1, "run lines 1 to 4 again on the clustered index",
             "Row_sel_get_clust_rec_for_mysql"),
            ("ret", 0, "return the one row"),
        ],
    },
    "beats": {
        "root": ["root"],
        "internal": ["loop", "child"],
        "leaf": ["child", "rec"],
        "unique": ["unique"],
        "covered": ["covers", "ret"],
        "pk-hop": ["covers", "pk"],
        "clus-root": ["clus", "root"],
        "clus-internal": ["clus", "loop", "child"],
        "clus-leaf": ["clus", "child", "rec"],
        "done": ["ret"],
    },
    "quiz": [
        {
            "question": ("uq_users_email and the clustered index are each 3 levels deep. How "
                         "many pages does SELECT id, email, first_name ... WHERE email = "
                         "'alice@example.com' read?"),
            "choices": [
                ("6: three in each tree", True,
                 "The unique record holds id 42, not a page address, so InnoDB walks the "
                 "clustered index from its root page to find first_name."),
                ("4: three, then one jump straight to the row's page", False,
                 "A secondary record stores the primary key value, and finding that key in "
                 "the clustered index starts again at its root page."),
                ("3: a unique lookup reads one tree", False,
                 "UNIQUE limits how many records can match, not which columns a record "
                 "holds, and first_name lives only in the clustered index."),
            ],
        },
        {
            "question": ("Why does the lookup stop after one record instead of reading the "
                         "next one to see whether it matches too?"),
            "choices": [
                ("uq_users_email is UNIQUE, so no other record can hold that email", True,
                 "MySQL and MariaDB both turn an equality on every column of a unique index "
                 "into a single-row lookup, which reads one record and never asks for a "
                 "next one."),
                ("The query has an implied LIMIT 1", False,
                 "There is no LIMIT here; the unique index is what guarantees at most one row."),
                ("A leaf page holds only one record", False,
                 "A 16 KiB leaf page holds hundreds of records; uniqueness, not page size, "
                 "rules out a second match."),
            ],
        },
        {
            "question": ("Can WHERE email IS NULL return more than one row through the "
                         "UNIQUE index uq_users_email?"),
            "choices": [
                ("Yes: a UNIQUE index accepts any number of NULLs", True,
                 "NULL never equals NULL, so many rows may store NULL in email, and InnoDB "
                 "keeps reading past the first match when the searched value is NULL."),
                ("No: UNIQUE allows a single NULL", False,
                 "MySQL and MariaDB both accept many rows with NULL in a UNIQUE column; only "
                 "a PRIMARY KEY forbids NULL."),
                ("Only when the index is covering", False,
                 "Covering decides which trees are read, not how many rows can match."),
            ],
        },
    ],
}


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (single-row unique lookup)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="rows">Rows in <code>users</code> table: <span class="value-pill" data-pill-for="rows">1000000</span></label>
      <input type="range" id="rows" name="rows" min="0" max="6" step="1" value="3">
      <div class="hint">Logarithmic: 1K, 10K, 100K, 1M, 10M, 100M, 1B</div>
    </div>

    <div class="control">
      <label for="covering">Covering unique index?</label>
      <select id="covering" name="covering">
        <option value="false" selected>No: SELECT id, email, first_name (clustered index walk)</option>
        <option value="true">Yes: SELECT id, email (index only)</option>
      </select>
      <div class="hint">uq_users_email holds (email, id). Asking for first_name adds a walk of the clustered index.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Single-row lookup on a UNIQUE secondary index\n"
            "SELECT id, email, first_name\n"
            "FROM   users\n"
            "WHERE  email = 'alice@example.com';   -- uq_users_email: UNIQUE (email)"
        ),
        note=(
            "EXPLAIN shows type const here (eq_ref when the email comes from another "
            "table in a join). MySQL reads a const table once while it makes the plan, "
            "so EXPLAIN ANALYZE shows Rows fetched before execution; an eq_ref lookup "
            "prints as Single-row index lookup. Switch Covering to drop first_name and "
            "the clustered index walk."
        ),
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The grey rectangles are B+tree pages of the UNIQUE index uq_users_email. The top row is the root page, the middle rows hold internal pages and the bottom row holds leaf pages.",
            "A red diamond 'query token' appears above the root page and goes down one page at a time. Each step is one page read; Next step shows one read at a time.",
            "When the token arrives at a page, the page pulses yellow. That's 'page is in the buffer pool now'.",
            "Because the index is UNIQUE, at most ONE leaf record can hold alice@example.com. A small chip under the leaf says so: no range scan, no next-record check.",
            "Covering mode: the leaf record (email, id) already has every column SELECT id, email asks for, an 'Index-only' badge appears, and the lookup stops there.",
            "Non-covering mode: first_name is not in the record, which stores the primary key (PK = 42). A dashed orange arrow links it to the clustered leaf that holds the row: the key, not a disk address, is the link, so InnoDB then walks the clustered tree from its root page.",
            "The simplified code beside the drawing highlights the lines each step runs.",
        ],
    )

    stage_html = f"""
<section class="stage" aria-labelledby="stage-h">
  <h2 id="stage-h" class="sr-only" style="position:absolute;left:-9999px">Unique key lookup animation</h2>
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
  <div class="stage-with-phases">
    <svg id="unique-svg" viewBox="0 0 800 400" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (single-row unique lookup)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Tree height {ht("Number of B+tree levels from the root page to a leaf page. InnoDB's high fan-out keeps this tiny: a billion-row table is only 4 levels deep.")}</p><p class="value" id="out-height">—</p></div>
    <div class="item"><p class="label">Unique index reads {ht("Pages read in uq_users_email to reach the one matching record: one per level, from the root page to the leaf page.")}</p><p class="value" id="out-index">—</p></div>
    <div class="item"><p class="label">Clustered index reads {ht("Pages read in the clustered index to get first_name. The unique record holds the primary key, so InnoDB walks the clustered index from its root page: one page per level. 0 when the index covers the query.")}</p><p class="value" id="out-fetch">—</p></div>
    <div class="item"><p class="label">Pages touched {ht("Unique index reads plus clustered index reads for this one-row lookup: O(log n) covering, O(2 log n) non-covering.")}</p><p class="value" id="out-total">—</p></div>
  </div>
  <div class="explanation" id="out-exp"></div>
  <div class="complexity-chart">
    <p class="chart-title">Unique lookup vs full scan (log–log, InnoDB fan-out ≈ 950)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — unique vs non-unique lookup</summary>
  <div class="body">
    <p><strong>Unique lookup</strong> returns at most one row for a key value.
    The optimizer labels this access path <code>const</code> when the value
    is a constant, as here, or <code>eq_ref</code> when it comes from another
    table in a join. MySQL and MariaDB read a const table once while they make
    the plan, so MySQL's EXPLAIN ANALYZE shows it as <em>Rows fetched before
    execution</em>; an eq_ref lookup prints as <em>Single-row index lookup</em>.
    Because the upper bound is 1, there's no sibling-leaf walk and no "stop
    when the next key changes" check. A UNIQUE index can still hold many
    NULLs, so <code>WHERE email IS NULL</code> is not a single-row lookup.</p>

    <p><strong>Non-covering unique lookup</strong>: the leaf record of
    <code>uq_users_email</code> holds <code>(email, id)</code>. To return
    <code>first_name</code>, InnoDB takes the primary key from the record and
    walks the clustered index from its root page to the leaf that holds the
    row, the same second walk as in the B+tree lesson.</p>

    <p><strong>Non-unique lookup</strong> (see the sibling lesson) can match
    many rows for one value. That means a range of leaf records on the
    secondary tree, and one clustered index lookup per match when
    non-covering. The I/O scales with the number of matches, not just with
    <code>log n</code>.</p>

    <p><strong>Covering unique index</strong>: when every column in the
    SELECT list is stored in the unique index's leaf record (InnoDB keeps the
    primary key there too), the lookup never touches the clustered B+tree.
    It costs the same as a primary key lookup: a single descent of
    <code>O(log n)</code> pages.</p>

    <p>Sources: MySQL 8.4 Reference Manual §8.2.1.1 (WHERE clause optimization),
    §17.6.2 (InnoDB Indexes).</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % (
        INNODB_PAGE_OVERHEAD_BYTES,
        INNODB_CHILD_POINTER_BYTES,
        INNODB_PAGE_SIZE_DEFAULT,
    )

    return _html.render_page(
        lesson_id="unique_lookup",
        title="Unique key lookup: single-row index lookup",
        subtitle=(
            "Descend a UNIQUE B+tree to exactly one leaf record; if the "
            "index is non-covering, walk the clustered index for the row."
        ),
        version_chip="MySQL 8.4 • MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
        guide=GUIDE,
    )
