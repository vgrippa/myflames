"""
Token-cost estimation and the raw-plan-vs-myflames savings comparison.

Why this module exists
----------------------
Pasting a raw ``EXPLAIN ANALYZE FORMAT=JSON`` plan into an LLM is token-expensive.
The JSON is deeply nested and repetitive (``cost_info``, ``used_columns``,
``attached_condition`` repeated per table), and the model burns tokens parsing
structure before it can reason. myflames distils the same plan into a compact,
source-grounded *digest* of just the facts an agent needs. This module makes the
difference concrete: it builds the two prompts you'd actually send, counts their
tokens, and prices the saving.

Counting accuracy
-----------------
By default this uses an **offline heuristic** so myflames stays stdlib-only with
zero dependencies. The heuristic is labelled as an estimate everywhere it
surfaces; the *ratio* between the two prompts is far more stable than the
absolute counts because both sides are measured identically.

For **exact** Claude token counts, pass an Anthropic-backed counter (see
:func:`make_counter` with ``exact=True``). That uses Anthropic's
``messages.count_tokens`` endpoint, which is the only accurate tokenizer for
Claude models. We deliberately do NOT vendor or use ``tiktoken`` — it is
OpenAI's tokenizer and undercounts Claude by ~15-20% on prose and more on JSON.
"""
import re


# ---------------------------------------------------------------------------
# Pricing (USD per 1,000,000 tokens). Source: Anthropic pricing, as of the
# date below. Update PRICING + PRICING_AS_OF together when prices change.
# Input price is what matters for a query plan (the plan is prompt input).
# ---------------------------------------------------------------------------

PRICING_AS_OF = "2026-05-26"
PRICING = {
    "claude-opus-4-8":   {"label": "Opus 4.8",   "input": 5.00, "output": 25.00},
    "claude-sonnet-4-6": {"label": "Sonnet 4.6", "input": 3.00, "output": 15.00},
    "claude-haiku-4-5":  {"label": "Haiku 4.5",  "input": 1.00, "output": 5.00},
}
# Sonnet is the realistic default for "I pasted a plan into a chat assistant".
DEFAULT_PRICING_MODEL = "claude-sonnet-4-6"

# The question a human (or agent) actually asks alongside the plan. Included on
# BOTH sides of the comparison so the before/after is the real prompt you'd
# send, not just the payload — keeps the saving honest.
DEFAULT_QUESTION = "Why is this query slow, and how do I make it faster?"


# ---------------------------------------------------------------------------
# Token estimation (offline heuristic)
# ---------------------------------------------------------------------------

# Pre-tokenization into runs of letters / digits / punctuation / whitespace.
# Loosely mirrors the pre-tokenization step BPE tokenizers run before merging,
# and handles both pretty-printed and minified JSON, unlike a naive whitespace
# split. ASCII-oriented: EXPLAIN identifiers are ASCII.
_ATOM_RE = re.compile(r"[A-Za-z_]+|[0-9]+|[^\sA-Za-z0-9_]+|\s+")


def estimate_tokens(text):
    """Approximate the number of BPE tokens a Claude-class model uses for *text*.

    Method: split into letter / digit / punctuation / whitespace runs, then
    estimate sub-tokens per run from typical BPE behavior (words ~ 5 chars/token,
    digits ~ 3/token, punctuation runs ~ 2/token, whitespace ~ 4/token). This is
    an *estimate* — exact counts depend on the model's tokenizer. Use
    ``--tokenizer claude`` / ``--tokenizer gpt`` when you need the real number.
    """
    if not text:
        return 0
    total = 0
    for match in _ATOM_RE.finditer(text):
        piece = match.group()
        first = piece[0]
        if first.isspace():
            total += len(piece) // 4
        elif first.isdigit():
            total += max(1, int(round(len(piece) / 3.0)))
        elif first.isalpha() or first == "_":
            total += max(1, int(round(len(piece) / 5.0)))
        else:
            total += max(1, int(round(len(piece) / 2.0)))
    return total


def _make_gpt_counter(model):
    """tiktoken-backed exact counter for GPT models. Keyless and offline
    (after a one-time vocab download). Returns ``(count_fn, label)`` or
    ``(None, fallback_label)`` if tiktoken/the encoding isn't available.

    tiktoken is OpenAI's tokenizer and is *exact* for GPT models (unlike for
    Claude, where it is wrong — that path uses Anthropic count_tokens).
    """
    try:
        import tiktoken
    except Exception:
        return None, "heuristic estimate (tiktoken not installed; pip install 'myflames[gpt]')"
    name = (model or "").lower()
    # cl100k_base = GPT-4 / 3.5; o200k_base = GPT-4o / 4.1 / 5 family (current).
    enc_name = "cl100k_base" if ("gpt-4-" in name or "gpt-3.5" in name or name in ("gpt-4", "gpt-3.5-turbo")) else "o200k_base"
    try:
        enc = tiktoken.get_encoding(enc_name)
    except Exception as exc:
        return None, "heuristic estimate (tiktoken encoding unavailable: {})".format(type(exc).__name__)

    def _count(text):
        return len(enc.encode(text or ""))

    return _count, "exact (tiktoken {})".format(enc_name)


def make_counter(model=DEFAULT_PRICING_MODEL, tokenizer="heuristic"):
    """Return ``(count_fn, method_label)``.

    ``tokenizer`` selects how tokens are counted:
    - ``"heuristic"`` (default): the offline stdlib estimate. No key, no network.
    - ``"claude"``: Anthropic ``messages.count_tokens`` (needs the ``anthropic``
      package + ``ANTHROPIC_API_KEY``).
    - ``"gpt"``: tiktoken, exact for GPT models, keyless (needs the ``tiktoken``
      package; ``pip install 'myflames[gpt]'``).

    Any path that can't be satisfied falls back to the heuristic and says so in
    the label — it never pretends a heuristic number is exact.
    """
    choice = tokenizer or "heuristic"

    if choice == "heuristic":
        return estimate_tokens, "heuristic estimate (offline)"

    if choice == "gpt":
        fn, label = _make_gpt_counter(model)
        return (fn, label) if fn else (estimate_tokens, label)

    # choice == "claude"
    try:
        import anthropic
    except Exception:
        return estimate_tokens, "heuristic estimate (anthropic package not installed)"

    try:
        client = anthropic.Anthropic()
        # The SDK does NOT validate auth at construction — a missing key only
        # raises when a request is made. count_tokens is a free endpoint, so a
        # one-shot probe here confirms the key works (and the server is
        # reachable) up front, instead of letting a TypeError escape mid-run.
        client.messages.count_tokens(model=model, messages=[{"role": "user", "content": "x"}])
    except Exception as exc:
        msg = str(exc).lower()
        if "api_key" in msg or "authentication" in msg or "x-api-key" in msg:
            reason = "no ANTHROPIC_API_KEY"
        else:
            reason = "count_tokens call failed ({})".format(type(exc).__name__)
        return estimate_tokens, "heuristic estimate ({})".format(reason)

    def _count(text):
        resp = client.messages.count_tokens(
            model=model, messages=[{"role": "user", "content": text}],
        )
        return resp.input_tokens

    return _count, "exact (Anthropic count_tokens, {})".format(model)


# ---------------------------------------------------------------------------
# Digest: the compact projection of a sidecar an agent actually needs
# ---------------------------------------------------------------------------

def _fmt_int(n):
    """Group thousands with commas without depending on locale."""
    try:
        return "{:,}".format(int(n))
    except (TypeError, ValueError):
        return str(n)


def _render_plan_tree(node, lines, prefix="", is_last=True, is_root=True):
    """Render the ``plan_tree`` index as a compact ASCII tree of short labels.

    One line per operator. Drops every per-node detail (cost_info, conditions,
    column lists) that bloats the raw JSON — only the structural skeleton an
    agent needs to reason about operator nesting.
    """
    if not isinstance(node, dict):
        return
    label = node.get("short_label") or node.get("folded_label") or "(op)"
    if is_root:
        lines.append(label)
        child_prefix = ""
    else:
        connector = "`- " if is_last else "|- "
        lines.append(prefix + connector + label)
        child_prefix = prefix + ("   " if is_last else "|  ")
    children = node.get("children") or []
    for i, child in enumerate(children):
        _render_plan_tree(
            child, lines, child_prefix, i == len(children) - 1, is_root=False
        )


def build_digest(payload):
    """Build a compact, token-cheap text digest from a sidecar *payload*.

    *payload* is the dict produced by
    :func:`myflames.output_sidecar.build_sidecar`. The digest is plain text
    (markdown-ish) designed to be pasted into a prompt or returned from an agent
    tool. It contains every *decision-relevant* fact (summary, warnings,
    suggestions, plan skeleton) and none of the JSON structure overhead.
    """
    out = []
    src = payload.get("source") or {}
    ps = payload.get("plan_summary") or {}

    engine = src.get("engine") or "unknown"
    ver = src.get("engine_version")
    engine_str = "{} {}".format(engine, ver) if ver else engine
    vitals = "{} ops".format(ps.get("operator_count", "?"))
    if ps.get("max_depth"):
        vitals += " | depth {}".format(ps["max_depth"])
    if ps.get("total_time_ms"):
        vitals += " | {} ms".format(ps["total_time_ms"])
    rows_sent = ps.get("rows_sent")
    rows_ex = ps.get("rows_examined_estimate")
    if rows_sent is not None or rows_ex is not None:
        vitals += " | rows {} sent / {} examined".format(
            _fmt_int(rows_sent or 0), _fmt_int(rows_ex or 0)
        )

    out.append("# Query plan analysis (myflames digest)")
    out.append("engine {} | {}".format(engine_str, vitals))
    out.append("")

    summary = payload.get("executive_summary")
    if summary:
        out.append("SUMMARY: " + summary)
        out.append("")

    warnings = payload.get("warnings") or []
    if warnings:
        out.append("WARNINGS ({}):".format(len(warnings)))
        for w in warnings:
            tag = "{}/{}".format(w.get("severity", "?"), w.get("category", "?"))
            line = "- [{}] {}".format(tag, w.get("text", "").strip())
            nodes = w.get("node_labels")
            if nodes:
                line += " (@ {})".format(", ".join(nodes))
            out.append(line)
        out.append("")

    suggestions = payload.get("suggestions") or []
    if suggestions:
        out.append("SUGGESTIONS ({}):".format(len(suggestions)))
        for s in suggestions:
            tag = "{}/{}".format(s.get("severity", "?"), s.get("category", "?"))
            line = "- [{}] {}".format(tag, s.get("action", "").strip())
            why = s.get("why")
            if why:
                line += " Why: " + why.strip()
            out.append(line)
        out.append("")

    index_suggestions = payload.get("index_suggestions") or []
    if index_suggestions:
        out.append("INDEXES:")
        for ix in index_suggestions:
            ddl = ix.get("ddl") or "({} on {})".format(
                ", ".join(ix.get("columns") or []), ix.get("table") or "?"
            )
            out.append("- " + ddl)
        out.append("")

    switches = payload.get("optimizer_switches") or []
    if switches:
        named = ["{}={}".format(sw.get("name"), sw.get("value")) for sw in switches]
        out.append("OPTIMIZER_SWITCHES: " + ", ".join(named))
        out.append("")

    plan_tree = payload.get("plan_tree")
    if plan_tree:
        out.append("PLAN:")
        tree_lines = []
        _render_plan_tree(plan_tree, tree_lines)
        out.extend(tree_lines)
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def build_compare_digest(compare_sidecar):
    """Build a compact, token-cheap text diff from a ``compare-1.0`` sidecar.

    *compare_sidecar* is the dict from
    :func:`myflames.output_compare_sidecar.build_compare_sidecar`. The digest
    answers the only question that matters after a change — did it help? — in a
    few hundred tokens instead of two full plans.
    """
    before = compare_sidecar.get("before") or {}
    after = compare_sidecar.get("after") or {}
    summary = compare_sidecar.get("summary") or {}
    deltas = compare_sidecar.get("deltas") or []

    out = ["# Plan comparison (myflames diff)"]
    tb = before.get("total_time_ms")
    ta = after.get("total_time_ms")
    pct = summary.get("time_delta_pct")
    head = "total time {} ms -> {} ms".format(tb, ta)
    if pct is not None:
        head += " ({:+.0f}%)".format(pct)
    head += " | {} improved, {} regressed, {} unchanged".format(
        summary.get("improvements", 0), summary.get("regressions", 0),
        summary.get("unchanged", 0),
    )
    out.append(head)
    out.append("")

    def _emit(group_label, classification):
        rows = [d for d in deltas if d.get("classification") == classification]
        if not rows:
            return
        # Biggest movers first.
        rows.sort(
            key=lambda d: abs((d.get("self_time_ms") or {}).get("change_pct") or 0),
            reverse=True,
        )
        out.append(group_label + ":")
        for d in rows:
            st = d.get("self_time_ms") or {}
            cp = st.get("change_pct")
            cp_str = " ({:+.0f}%)".format(cp) if cp is not None else ""
            out.append("- {}: {} -> {} ms{}".format(
                d.get("short_label", "(op)"), st.get("before"), st.get("after"), cp_str,
            ))

    _emit("REGRESSED", "regressed")
    _emit("IMPROVED", "improved")
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Prompt builders — the two things you'd actually paste into an AI
# ---------------------------------------------------------------------------

def build_raw_prompt(raw_plan_text, question=DEFAULT_QUESTION):
    """The 'before': the raw EXPLAIN JSON plus the user's question."""
    return (
        "Here is the EXPLAIN ANALYZE FORMAT=JSON output for a MySQL/MariaDB query.\n"
        + question.strip() + "\n\n"
        + (raw_plan_text or "").strip() + "\n"
    )


def build_digest_prompt(digest_text, question=DEFAULT_QUESTION):
    """The 'after': the myflames digest plus the same question."""
    return (
        "Here is a myflames analysis of a MySQL/MariaDB query plan.\n"
        + question.strip() + "\n\n"
        + (digest_text or "").strip() + "\n"
    )


# ---------------------------------------------------------------------------
# Comparison + pricing
# ---------------------------------------------------------------------------

def _input_cost(tokens, model):
    price = PRICING.get(model, PRICING[DEFAULT_PRICING_MODEL])
    return tokens / 1000000.0 * price["input"]


def compare(raw_prompt, digest_prompt, count=None, method="heuristic estimate (offline)"):
    """Compare token cost of the raw-plan prompt vs the digest prompt.

    *count* is a token-counting function (defaults to the offline heuristic).
    *method* is a human label describing how counts were produced.

    Returns a dict with stable keys (machine-readable), including a per-model
    USD cost breakdown for the input tokens.
    """
    count = count or estimate_tokens
    raw_tokens = count(raw_prompt)
    digest_tokens = count(digest_prompt)
    saved = raw_tokens - digest_tokens
    reduction = (saved / raw_tokens * 100.0) if raw_tokens else 0.0
    ratio = (raw_tokens / digest_tokens) if digest_tokens else 0.0

    cost_by_model = {}
    for model_id, price in PRICING.items():
        raw_usd = _input_cost(raw_tokens, model_id)
        digest_usd = _input_cost(digest_tokens, model_id)
        cost_by_model[model_id] = {
            "label": price["label"],
            "raw_usd": round(raw_usd, 6),
            "digest_usd": round(digest_usd, 6),
            "saved_usd": round(raw_usd - digest_usd, 6),
            "saved_usd_per_1k_queries": round((raw_usd - digest_usd) * 1000, 2),
        }

    return {
        "raw_tokens": raw_tokens,
        "digest_tokens": digest_tokens,
        "raw_chars": len(raw_prompt or ""),
        "digest_chars": len(digest_prompt or ""),
        "tokens_saved": saved,
        "reduction_pct": round(reduction, 1),
        "ratio": round(ratio, 1),
        "method": method,
        "pricing_as_of": PRICING_AS_OF,
        "cost_by_model": cost_by_model,
    }


def format_report(comparison, pricing_model=DEFAULT_PRICING_MODEL):
    """Render a :func:`compare` result as a human-readable terminal report."""
    c = comparison
    # GPT/tiktoken counts must not be priced against the Claude tables (GPT
    # tokens × Claude $/token is meaningless). Show a tokens-only report with a
    # pointer to the user's own provider pricing. Only when the counts are
    # ACTUALLY exact-GPT — a heuristic fallback whose label mentions tiktoken
    # must take the normal (estimated-cost) path.
    if c.get("method", "").startswith("exact (tiktoken"):
        lines = [
            "Token cost: pasting a raw EXPLAIN plan into an AI vs the myflames digest",
            "=" * 72,
            "  What you'd paste                          Tokens",
            "  BEFORE  raw plan JSON + your question  {:>9}".format(_fmt_int(c["raw_tokens"])),
            "  AFTER   myflames digest + your question{:>9}".format(_fmt_int(c["digest_tokens"])),
            "  " + "-" * 52,
            "  SAVED                                  {:>9}   ({}% fewer, {}x smaller)".format(
                _fmt_int(c["tokens_saved"]), c["reduction_pct"], c["ratio"]),
            "",
            "  Token counts: {} (exact for GPT, keyless).".format(c["method"]),
            "  Apply your provider's per-token pricing to the saving above.",
        ]
        return "\n".join(lines) + "\n"
    cbm = c["cost_by_model"]
    pm = cbm.get(pricing_model) or cbm[DEFAULT_PRICING_MODEL]
    lines = [
        "Token cost: pasting a raw EXPLAIN plan into an AI vs the myflames digest",
        "=" * 72,
        "  What you'd paste                          Tokens     Cost ({} input)".format(pm["label"]),
        "  BEFORE  raw plan JSON + your question  {:>9}     ${:.4f}".format(
            _fmt_int(c["raw_tokens"]), pm["raw_usd"]
        ),
        "  AFTER   myflames digest + your question{:>9}     ${:.4f}".format(
            _fmt_int(c["digest_tokens"]), pm["digest_usd"]
        ),
        "  " + "-" * 68,
        "  SAVED                                  {:>9}     ${:.4f}   ({}% fewer, {}x smaller)".format(
            _fmt_int(c["tokens_saved"]), pm["saved_usd"], c["reduction_pct"], c["ratio"]
        ),
        "",
        "  Per 1,000 such queries you save ~${:.2f} in input tokens ({}).".format(
            pm["saved_usd_per_1k_queries"], pm["label"]
        ),
        "",
        "  BEFORE cost by model (input tokens):",
    ]
    for model_id in ("claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5"):
        m = cbm.get(model_id)
        if not m:
            continue
        lines.append(
            "    {:<11} ${:.4f}  ->  digest ${:.4f}   save ${:.4f}".format(
                m["label"], m["raw_usd"], m["digest_usd"], m["saved_usd"]
            )
        )
    lines += [
        "",
        "  Token counts: {}.".format(c["method"]),
        "  Prices as of {}. Both sides include the question framing.".format(c["pricing_as_of"]),
        "  For exact Claude counts: --tokenizer claude (needs `pip install",
        "  myflames[tokens]` and ANTHROPIC_API_KEY). tiktoken is wrong for Claude;",
        "  use --tokenizer gpt only for GPT models.",
    ]
    return "\n".join(lines) + "\n"
