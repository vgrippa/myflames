"""Lesson: Covering index — why "Using index" in EXPLAIN is the goal.

A covering index is a secondary index that contains every column the
query needs, so MySQL can satisfy the SELECT entirely from the index
without doing a second lookup into the clustered (primary) index.

Three acts that build intuition:

Act 1 — **Without a covering index**. Plan uses a secondary index on
        ``(status)`` to find rows. Each entry holds (status, id); the id
        is looked up in the clustered index to read ``email``. That's
        two reads per matching row.

Act 2 — **With a covering index**. CREATE INDEX idx_status_email ON
        users (status, email). EXPLAIN's Extra column now says "Using
        index". One read per row.

Act 3 — **The InnoDB PK-append trick**. Secondary indexes on InnoDB
        automatically include the primary-key columns — so
        ``idx_status(status)`` on a table with PK ``id`` is
        effectively ``(status, id)``. A query like
        ``SELECT id FROM users WHERE status = 'active'`` is
        *already* covered, even though the user never added id to
        the index definition. This is the #1 covering-index myth we
        want to correct.

Plus a sidebar: "Using index" (covering) vs "Using index condition"
(ICP, not covering). The drawing is one SVG so the index entries and the
clustered rows share one scale and every label fits its box.

MySQL internals verified against server source:
  * "Using index" string comes from ``ET_USING_INDEX`` in
    sql/opt_explain.cc:1615, pushed when
    ``table->key_read || tab->keyread_optim()`` is true — i.e. the
    read can be satisfied entirely from the index.
  * "Using index condition" is ``ET_USING_INDEX_CONDITION`` — a
    different mechanism (predicate pushed to the storage engine
    layer). Both engines skip ICP when the chosen index is covering
    (make_join_readinfo in sql/sql_select.cc of both engines checks
    covering_keys before push_index_cond), so it always means a
    clustered lookup follows.
  * InnoDB secondary indexes always include the clustered-index
    unique columns (``clust_index->n_uniq``, see
    storage/innobase/dict/dict0dict.cc:3150
    ``dict_index_build_internal_non_clust``): that's how the
    PK-append behavior is implemented.
"""
from .. import _html


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). covering_index.js marks each
# act's plan, each entry read and the end of each range with tl.beat(kind).
# Function names were checked in mysql-server (8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Index lookup: covering or not",
        "lines": [
            ("plan", 0, "covering = the entry holds every column the query needs"),
            ("seek", 0, "find the first index entry with status = 'active'"),
            ("loop", 0, "for each entry, while entry.status = 'active':"),
            ("if", 1, "if covering:"),
            ("send-entry", 2, "send the columns straight from the entry (Using index)"),
            ("else", 1, "else:"),
            ("hop", 2, "look up entry.id in the clustered index",
             "Row_sel_get_clust_rec_for_mysql"),
            ("send-row", 2, "send email from the full row"),
            ("stop", 0, "the next entry is not 'active': the range ends",
             "row_search_mvcc"),
        ],
    },
    "beats": {
        "plan": ["plan", "seek"],
        "lookup": ["loop", "else", "hop", "send-row"],
        "covered": ["loop", "if", "send-entry"],
        "stop": ["stop"],
    },
    "quiz": [
        {
            "question": ("With only idx_status (status), SELECT email FROM users WHERE "
                         "status = 'active' matches 6 rows. How many times does InnoDB look up "
                         "a row in the clustered index?"),
            "choices": [
                ("6, once per matching entry", True,
                 "email is not in idx_status, so each matching entry hands its primary key "
                 "id to its own lookup in the clustered index."),
                ("0, the index already found the rows", False,
                 "The index finds which rows match, but email lives only in the full row, "
                 "so each one is fetched from the clustered index."),
                ("Once, for all 6 rows together", False,
                 "Each entry carries a different id, and each id is looked up on its own."),
            ],
        },
        {
            "question": ("Why does idx_status (status) cover SELECT id FROM users WHERE "
                         "status = 'active', even though id is not in the index definition?"),
            "choices": [
                ("InnoDB stores the primary key in every secondary index entry", True,
                 "A secondary entry is its indexed columns plus the primary key, which is "
                 "also how a non-covering lookup finds the full row."),
                ("The server caches recent id values in memory", False,
                 "There is no such cache; id is stored in each idx_status entry itself."),
                ("Reading id from the clustered index costs nothing", False,
                 "A clustered index read is exactly the lookup a covering index avoids, "
                 "and it is never free."),
            ],
        },
        {
            "question": "EXPLAIN shows Using index condition. Is the index covering the query?",
            "choices": [
                ("No, the rows that pass are still read from the clustered index", True,
                 "MySQL and MariaDB skip index condition pushdown when the index covers the "
                 "query, so seeing it means full rows are still fetched."),
                ("Yes, it means the same as Using index", False,
                 "Using index condition is index condition pushdown: part of the WHERE "
                 "clause is checked on the entry before the full row is read."),
            ],
        },
    ],
}


def render():
    query_card_html = _html.query_card(
        "CREATE TABLE users (id INT PRIMARY KEY, email VARCHAR(64),\n"
        "                    status VARCHAR(16), country CHAR(2),\n"
        "                    KEY idx_status (status));\n"
        "-- Acts 1 and 2 (Act 2 first adds an index):\n"
        "SELECT email FROM users WHERE status = 'active';\n"
        "CREATE INDEX idx_status_email ON users (status, email);\n"
        "-- Act 3, with idx_status:\n"
        "SELECT id FROM users WHERE status = 'active';",
        note=(
            "One table, three plans. Watch where each returned value comes from: the "
            "index entry alone, or a second lookup in the clustered index. Act 3 shows "
            "the primary key that InnoDB keeps in every secondary index entry."
        ),
    )
    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "Left: the leaf entries of the secondary index, sorted by status and then id. "
            "Right: the leaf rows of the clustered index (the table itself), sorted by id, "
            "with every column.",
            "The outlined entry is the one being read. In Act 1 its id travels along an "
            "orange arrow to the clustered index, and the row it fetches turns yellow.",
            "Green entries answered the query by themselves (Acts 2 and 3). Grey rows on "
            "the right were never read.",
            "The line under the drawing counts index entries read and clustered rows read. "
            "EXPLAIN's Extra column says <code>Using index</code> only when no clustered "
            "read is needed; <code>Using index condition</code> is a different feature.",
            "Next step reads one entry at a time; the simplified code beside the drawing "
            "highlights the lines each step runs.",
        ],
    )
    # No inputs: the lesson replays one fixed table, so Explore offers the
    # step slider only. The section names the table and its indexes.
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Table and indexes (fixed for this lesson)</h2>
  <p class="hint">users: 9 rows, PRIMARY KEY (id), KEY idx_status (status). Act 2 adds
  KEY idx_status_email (status, email). Every InnoDB secondary index entry also holds the
  primary key id.</p>
</section>
"""
    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
  <div class="stage-with-phases">
    <svg id="ci-svg" viewBox="0 0 800 460" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Live plan stats</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Phase</p><p class="value" id="out-phase">—</p></div>
    <div class="item"><p class="label">EXPLAIN Extra {ht("The Extra column text MySQL prints for this access. &#39;Using index&#39; = covering; anything else usually means a second lookup per row.")}</p><p class="value" id="out-extra">—</p></div>
    <div class="item"><p class="label">Index reads {ht("Secondary index entries read, including the first entry past the range (status = 'inactive'), which is how the read knows to stop.")}</p><p class="value" id="out-index-reads">—</p></div>
    <div class="item"><p class="label">Clustered (table) reads {ht("Rows read from the primary/clustered index. Zero means the plan is covering.")}</p><p class="value hot" id="out-table-reads">—</p></div>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — the "Using index" vs "Using index condition" trap</summary>
  <div class="body">
    <p>Two strings in EXPLAIN's <code>Extra</code> column look similar
    and are routinely confused. They mean different things:</p>

    <h3><code>Using index</code> — covering</h3>
    <p>The read is satisfied entirely from the secondary index. No
    lookup into the clustered index happens. In MySQL source this is
    pushed when <code>table-&gt;key_read</code> is true (see
    <code>sql/opt_explain.cc</code> around line 1615). A covering
    query's cost is proportional to the matching index entries —
    never to the table size.</p>

    <h3><code>Using index condition</code> — ICP (not covering)</h3>
    <p>Index Condition Pushdown. Part of the WHERE clause is checked on
    the index entry inside the storage engine, so entries that fail it
    never cost a clustered-index lookup. The entries that pass still do.
    MySQL and MariaDB both skip ICP when the chosen index covers the
    query, so <code>Using index condition</code> always means full rows
    are still being read. Useful, but not the same as covering.</p>
    <p>MySQL's <code>EXPLAIN FORMAT=TREE</code> says it in words:
    <code>Covering index lookup on users using idx_status_email</code>
    versus <code>Index lookup on users using idx_status</code>.</p>

    <h3>The InnoDB PK-append property</h3>
    <p>InnoDB's clustered-index primary key is silently appended to
    every secondary index. That's implemented in
    <code>storage/innobase/dict/dict0dict.cc</code>
    (<code>dict_index_build_internal_non_clust</code>): the internal
    index struct is created with <code>index-&gt;n_fields + 1 +
    clust_index-&gt;n_uniq</code> fields — the +<em>n_uniq</em> is
    the PK columns being appended. So:</p>

    <pre><code>CREATE TABLE users (id INT PRIMARY KEY, status VARCHAR(16), email VARCHAR(64));
CREATE INDEX idx_status ON users (status);
-- This index physically stores (status, id) — the id was appended for free.

SELECT id FROM users WHERE status = 'active';
-- EXPLAIN: Using index.  ← covering, even though `id` isn't in
--                           the index definition.
</code></pre>

    <p>This property is why defining extra indexes "just for the id"
    is almost always wasted effort on InnoDB, in MySQL and in MariaDB.
    MyISAM and Aria secondary indexes point at a row position instead
    of holding the primary key, so on those engines write the primary
    key columns into the index yourself if you need them covered.</p>
  </div>
</details>
"""

    return _html.render_page(
        lesson_id="covering_index",
        title="Covering index: why \"Using index\" in EXPLAIN is the goal",
        subtitle=(
            "Three acts: non-covering plan, covering plan, and the "
            "InnoDB PK-append property that silently covers queries "
            "you didn't realize were covered."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS_TEMPLATE,
        guide=GUIDE,
    )
