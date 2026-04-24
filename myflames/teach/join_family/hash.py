"""Lesson: Hash join with grace-hash spill (MySQL 8.4).

Shows real data: 6 concrete department rows (id + name) hashed into
buckets, then 8 named employees probed through. Each bucket shows its
contents after the build phase so the user can see WHY Alice lands in
bucket 3 and matches "Eng". When a probe row arrives, the matching
department row flashes green with a MATCH label.
"""
from .. import _html
from .._cost_model import JOIN_BUFFER_SIZE_DEFAULT


_LESSON_JS_TEMPLATE = _html.load_lesson_js(__file__)


def render() -> str:
    controls_html = f"""
<section class="controls">
  <h2>Parameters (MySQL 8.4 hash join: employees ⋈ departments)</h2>
  <div class="control-grid">

    <div class="control">
      <label for="build_rows"><code>departments</code> rows (build side): <span class="value-pill" data-pill-for="build_rows">500</span></label>
      <input type="range" id="build_rows" name="build_rows" min="100" max="5000000" step="100" value="500">
      <div class="hint">Smaller input → MySQL picks this side for the hash table.</div>
    </div>

    <div class="control">
      <label for="probe_rows"><code>employees</code> rows (probe side): <span class="value-pill" data-pill-for="probe_rows">100000</span></label>
      <input type="range" id="probe_rows" name="probe_rows" min="1000" max="100000000" step="1000" value="100000">
      <div class="hint">Larger input — streamed through the built hash table once.</div>
    </div>

    <div class="control">
      <label for="row_size">Row size (bytes): <span class="value-pill" data-pill-for="row_size">200</span></label>
      <input type="range" id="row_size" name="row_size" min="32" max="4096" step="32" value="200">
    </div>

    <div class="control">
      <label for="jbs">join_buffer_size (bytes): <span class="value-pill" data-pill-for="jbs">262144</span></label>
      <input type="range" id="jbs" name="jbs" min="8192" max="134217728" step="8192" value="{JOIN_BUFFER_SIZE_DEFAULT}">
      <div class="hint">MySQL 8.4 default: 256 KiB. Bigger → fewer spills.</div>
    </div>

  </div>
</section>
"""

    query_card_html = _html.query_card(
        sql=(
            "-- Non-indexed equi-join executed by MySQL 8.4 hash join\n"
            "SELECT e.name, d.name AS department\n"
            "FROM   employees  e\n"
            "JOIN   departments d  ON  e.dept_id = d.id\n"
            "WHERE  e.active = 1;   -- d.id has no usable index → hash join"
        ),
        note="MySQL picks the smaller input (departments) as the build side. The larger input (employees) is streamed through in one pass."
    )

    explainer_html = _html.explainer(
        "What you'll see in the animation — with real data",
        [
            "Phase 1 — build: 6 department rows fly from the left into hash buckets. Each pill is labelled with the actual row (e.g. 'id=3 Eng'). The hash function decides which bucket: hash(dept.id) % 6. After the build, each bucket shows the department rows it holds (e.g. bucket [3] holds 'Eng, HR').",
            "Phase 2 — probe: 8 employee rows stream from the right. Each pill is labelled (e.g. 'Alice dept=3'). MySQL computes hash(3) % 6 = bucket [3], and looks inside that bucket for a department row with id=3.",
            "When a match is found — e.g. Alice's dept_id=3 matches 'Eng' (id=3) in bucket [3] — the bucket flashes green with 'MATCH ✓'. That joined pair (Alice + Eng) is sent to the client.",
            "Two rows land in the same bucket when hash(key) produces the same index. But same bucket ≠ same key — MySQL still checks actual values. The hash table just narrows the search from 'scan all 6 departments' to 'check 1 or 2 rows in one bucket'.",
            "Total work: one pass through departments (build) + one pass through employees (probe) = O(n + m). If the hash table exceeds join_buffer_size, a red spill banner appears.",
        ],
    )

    stage_html = f"""
<section class="stage">
  {query_card_html}
  {explainer_html}
  {_html.stage_toolbar("Ready — press Play")}
  <div class="stage-with-phases">
    <svg id="hash-svg" viewBox="0 0 800 380" xmlns="http://www.w3.org/2000/svg"></svg>
    {_html.phase_nav()}
  </div>
</section>
"""

    ht = _html.help_tip
    readout_html = f"""
<section class="readout">
  <h2>Cost readout (MySQL 8.4 hash join)</h2>
  <div class="readout-grid">
    <div class="item"><p class="label">Build-side memory {ht("How much RAM the hash table needs. MySQL picks the smaller input as the build side and hashes it into join_buffer_size. Includes ~40% overhead for bucket chains.")}</p><p class="value" id="out-build">—</p></div>
    <div class="item"><p class="label">Fits in join_buffer_size? {ht("If Yes, everything runs in memory — fast single-pass. If No, MySQL spills both inputs to disk and re-reads them, roughly doubling the I/O.")}</p><p class="value" id="out-fits">—</p></div>
    <div class="item"><p class="label">Spilled to disk? {ht("When the build side is too big, MySQL partitions both inputs to disk files, then re-reads each partition for build + probe. This is called grace-hash.")}</p><p class="value" id="out-spilled">—</p></div>
    <div class="item"><p class="label">Partitions {ht("Number of on-disk chunks when spilling. Each partition's build side must fit in join_buffer_size. More partitions = more disk I/O passes.")}</p><p class="value" id="out-parts">—</p></div>
    <div class="item"><p class="label">Phases {ht("2 phases when in-memory (build + probe). 4 when spilling (partition-write + partition-read + build + probe).")}</p><p class="value" id="out-phases">—</p></div>
    <div class="item"><p class="label">Complexity {ht("O(departments + employees) = O(n + m) when in-memory. O(2·(n + m)) when spilling to disk.")}</p><p class="value" id="out-complexity">O(depts + emps) = O(n + m)</p></div>
  </div>
  <div class="explanation" id="out-explanation"></div>
  <div class="complexity-chart">
    <p class="chart-title">Row comparisons vs probe size (log–log, build side fixed)</p>
    <svg id="complexity-chart" viewBox="0 0 560 200" xmlns="http://www.w3.org/2000/svg"></svg>
  </div>
</section>
"""

    learn_more_html = """
<details class="learn-more">
  <summary>Learn more — what happens when the build side doesn't fit?</summary>
  <div class="body">
    <p>MySQL 8.4 allocates the hash table out of <code>join_buffer_size</code>.
    If the whole build side fits, you get a single-pass hash join:
    <strong>phase 1</strong> builds the hash table, <strong>phase 2</strong>
    streams the probe rows through and emits matches. O(build + probe).</p>

    <p>If it doesn't fit, MySQL falls back to <em>grace hash</em>: both
    inputs are partitioned by a hash function onto disk, one file per
    partition. Each partition is then built + probed independently. Total
    I/O roughly doubles (write both inputs, read both inputs again), and
    the complexity grows to O(2·(build + probe)).</p>

    <p><strong>Why a hash table?</strong> Without one, matching Alice
    (dept_id=3) against departments would require scanning all 6 rows —
    O(n). With the hash table, you compute hash(3) % 6 → bucket [3] and
    check only the 1–2 rows in that bucket. That's O(1) per probe row,
    which is what makes the whole join O(n + m).</p>

    <p>Source: MySQL 8.4 Reference Manual §10.2.1.4 "Hash Join
    Optimization".</p>
  </div>
</details>
"""

    lesson_js = _LESSON_JS_TEMPLATE % JOIN_BUFFER_SIZE_DEFAULT

    return _html.render_page(
        lesson_id="hash",
        title="Hash join — build, probe, and grace-hash spill",
        subtitle=(
            "MySQL 8.4's default for non-indexed equi-joins. Watch real "
            "department rows hash into buckets, then see employees find "
            "their match in one bucket lookup."
        ),
        controls_html=controls_html,
        stage_html=stage_html,
        readout_html=readout_html,
        learn_more_html=learn_more_html,
        lesson_js=lesson_js,
    )
