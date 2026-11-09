"""Lesson: Non-Unique Key Lookup.

Teaches the full operator flow, not only the label:
1) descend the secondary B+tree (root page -> internal page -> leaf page),
2) read the matching leaf entries one by one, in (country, id) order,
3) for non-covering plans, look each entry's primary key up in the
   clustered index (one lookup per entry, interleaved with the entry reads),
4) stop at the first entry with another key.
"""
from .. import _html


_LESSON_JS = _html.load_lesson_js(__file__)

# Guided steps (see _html.normalize_guide). non_unique_lookup.js marks one
# step per page of the descent, one per matching entry and the stop at the
# next key with tl.beat(kind). Function names are on
# _html.VERIFIED_SERVER_FUNCTIONS (checked in mysql-server 8.4.9 and 9.7.0).
GUIDE = {
    "code": {
        "title": "Non-unique index lookup in InnoDB",
        "lines": [
            ("root", 0, "page = root page of idx_users_country", "btr_cur_search_to_nth_level"),
            ("loop", 0, "while page is not a leaf page:"),
            ("child", 1, "page = child page whose key range holds 'US'",
             "page_cur_search_with_match"),
            ("first", 0, "entry = first entry with country = 'US'"),
            ("while", 0, "while entry.country = 'US':"),
            ("covers", 1, "if the query needs a column the entry lacks (name):"),
            ("fetch", 2, "row = clustered index lookup by entry.id",
             "Row_sel_get_clust_rec_for_mysql"),
            ("send", 1, "send the row"),
            ("next", 1, "entry = next entry (on the next leaf page at the end)",
             "ha_innobase::index_next_same"),
            ("stop", 0, "entry has another country: the lookup ends"),
        ],
    },
    "beats": {
        "root": ["root"],
        "internal": ["loop", "child"],
        "leaf": ["child", "first"],
        "row": ["while", "covers", "fetch", "send", "next"],
        "row-covered": ["while", "covers", "send", "next"],
        "stop": ["while", "stop"],
    },
    "quiz": [
        {
            "question": ("country = 'US' matches 20,000 entries of idx_users_country, which "
                         "holds only (country, id). How many clustered index lookups does "
                         "SELECT id, name, country make?"),
            "choices": [
                ("20,000, one per matching entry", True,
                 "Each entry gives one id, and name lives only in the row, so InnoDB looks "
                 "each id up in the clustered index before it sends the row."),
                ("1, because all US rows sit together", False,
                 "The rows are stored in id order in the clustered index, not by country, so "
                 "each US row is looked up on its own."),
                ("0, because the index found them", False,
                 "The index finds which ids match, but name is not in it, so every match "
                 "still needs its row."),
            ],
        },
        {
            "question": "How does the lookup know it has read the last 'US' entry?",
            "choices": [
                ("It reads the next entry and sees another country", True,
                 "Entries are sorted by (country, id), so the first entry with another "
                 "country ends the run, and InnoDB stops there."),
                ("It counts down from the optimizer's row estimate", False,
                 "Row estimates are only used to choose a plan; the lookup stops on the data "
                 "it actually reads."),
                ("It stops at the end of the first leaf page", False,
                 "The 'US' entries can span many leaf pages; the lookup follows the leaf pages "
                 "in order until the key changes."),
            ],
        },
        {
            "question": ("Which index lets SELECT id, name, country ... WHERE country = 'US' "
                         "skip every clustered index lookup?"),
            "choices": [
                ("An index on (country, name)", True,
                 "Its entries hold country and name, and InnoDB adds the primary key id to "
                 "every secondary index, so each entry has every column the query asks for."),
                ("An index on (name)", False,
                 "Its entries are sorted by name, so it cannot jump to the 'US' entries and "
                 "would have to read them all."),
                ("A UNIQUE index on country", False,
                 "Many users share a country, so a UNIQUE index on country cannot be created "
                 "on this table."),
            ],
        },
    ],
}


def render() -> str:
    controls_html = """
<section class="controls" aria-labelledby="controls-h">
  <h2 id="controls-h">Parameters (non-unique lookup)</h2>
  <div class="control-grid">
    <div class="control">
      <label for="rows">Rows in table</label>
      <input type="range" id="rows" name="rows" min="1000" max="10000000" step="1000" value="1000000">
      <div class="hint">Table rows: <span data-pill-for="rows">1000000</span></div>
    </div>
    <div class="control">
      <label for="selectivity">Leaf-range selectivity (%)</label>
      <input type="range" id="selectivity" name="selectivity" min="0.1" max="40" step="0.1" value="2.0">
      <div class="hint">Percent of table rows whose leaf entries match <code>country='US'</code>: <span data-pill-for="selectivity">2.0</span>%. The drawing shows 6 of them.</div>
    </div>
    <div class="control">
      <label for="covering">Covering index?</label>
      <select id="covering" name="covering">
        <option value="false" selected>No: SELECT id, name, country (name is in the row)</option>
        <option value="true">Yes: SELECT id, country (the index has both)</option>
      </select>
      <div class="hint">Covering avoids the clustered index lookup per match.</div>
    </div>
  </div>
</section>
"""
    query_card_html = _html.query_card(
        "SELECT id, name, country\nFROM users\nWHERE country = 'US';",
        "With index idx_users_country(country), EXPLAIN ANALYZE shows Index lookup on users using idx_users_country (Covering index lookup when the index holds every column; a range condition shows as Index range scan): descend the secondary B+tree, read the matching leaf entries, and look each entry's primary key up in the clustered index.",
    )
    explainer_html = _html.explainer(
        "What you'll see in the animation",
        [
            "The pill is your predicate value (country='US') moving down the root page, an internal page and the leaf pages of idx_users_country. The page it reaches turns yellow.",
            "At the leaf pages, one key value maps to many entries, sorted by (country, id). Each entry stores the row's primary key (id), not a disk address. The drawing shows 6 of them.",
            "Non-covering path: name is not in the entry, so each entry's id is looked up in the clustered index (the dot travels right) before the next entry is read.",
            "Covering path: SELECT id, country needs nothing else, so the rows are sent straight from the entries.",
            "The run ends at the first entry with another country (UY). Next step shows one page or one entry at a time, and the simplified code beside the drawing highlights the lines each step runs.",
        ],
    )
    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready: press Next step, or Play to run it all")}
  <div class="stage-with-phases">
    <svg id="lookup-svg" viewBox="0 0 860 470" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""
    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (B+tree descent + leaf entries + clustered index lookups)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">B+tree height {ht("Approximate number of index pages from the root page to the leaf page where the 'US' entries start.")}</p><p class="value" id="out-height">—</p></div>
    <div class="item"><p class="label">Descent page reads {ht("Root page + internal pages + the first leaf page with a 'US' entry, read before the first match.")}</p><p class="value" id="out-descent">—</p></div>
    <div class="item"><p class="label">Matched leaf entries {ht("Index entries with country = 'US'. Non-unique means this can be many: rows times the selectivity.")}</p><p class="value" id="out-match">—</p></div>
    <div class="item"><p class="label">Index reads {ht("Descent pages plus matching entries read: a rough count of index work.")}</p><p class="value" id="out-index">—</p></div>
    <div class="item"><p class="label">Clustered index lookups {ht("For a non-covering lookup, one clustered index lookup per matching entry, each a walk from the root page to the leaf page with that id. 0 when the index covers the query.")}</p><p class="value" id="out-fetch">—</p></div>
    <div class="item"><p class="label">Total reads (rough) {ht("Index reads plus clustered index lookups. A covering index removes the lookups.")}</p><p class="value" id="out-total">—</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Work vs table size (log–log, selectivity fixed)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""
    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — what actually happens in the background</summary>
  <div class="body">
    <p>For <code>country='US'</code>, InnoDB first descends the secondary B+tree (root page, internal pages, first
    leaf page with a 'US' entry). Then it reads the following entries, crossing to the next leaf page when one ends,
    while the key still matches US. The first entry with another country ends the run.</p>
    <p>Each leaf entry holds <code>(country, id)</code>: InnoDB keeps the primary key in every secondary index, and
    that key is the row's row-id. On non-covering plans InnoDB looks each id up in the clustered PRIMARY tree,
    from its root page, to get the other columns, one entry at a time.</p>
    <p>Those lookups are the expensive part: one per matching row. If the index is covering, the rows come straight
    from the entries and the clustered index is never read.</p>
  </div>
</details>
"""
    return _html.render_page(
        lesson_id="non_unique_lookup",
        title="Non-unique key lookup: index hits that return many rows",
        subtitle="Understand how B+tree traversal, leaf-range scanning, and row-id fetches compose the real work of this operator.",
        version_chip="MySQL 8.4 • MariaDB 11.4",
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=_LESSON_JS,
        guide=GUIDE,
    )

