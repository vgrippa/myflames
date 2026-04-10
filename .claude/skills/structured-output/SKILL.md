---
name: structured-output
description: Makes myflames outputs consumable by AI agents and external tools without OCR'ing SVG raster text. Owns the JSON sidecar schema, JSON-LD in HTML heads, semantic landmarks, and every decision about what gets serialized where. Use when adding new data to an output, designing a new integration point, or deciding whether a piece of information should live in the SVG, the HTML, or a JSON file.
---

# Structured Output for Machine Consumption

Every human-readable artifact myflames produces should have a **machine-readable counterpart** — either a sidecar file, an embedded JSON-LD block, or semantic HTML landmarks — so an AI agent or external tool can answer "what's wrong with this query?" without parsing SVG `<text>` nodes or running OCR on a screenshot. The goal is: **one source of truth per data point, two projections (human + machine)**.

## Principles

1. **Sidecar first, embed second.** Prefer emitting a separate `.json` file next to every `.svg` / `.html` (e.g. `plan-47.svg` + `plan-47.json`). It's versionable independently, cacheable, diffable, and trivially consumed by curl+jq. Only embed JSON-LD in the HTML `<head>` when the artifact must be fully self-contained for sharing.

2. **The SVG is a picture, not a document.** SVG `<text>` nodes are for labels rendered on top of the visualization. They are NOT a machine-readable channel. If a piece of information needs to be consumed by something that isn't a human eye, it belongs in the sidecar or the HTML — never only in the SVG.

3. **Versioned, stable schemas.** Every JSON sidecar carries `"schema_version"`. Schema changes are either additive (safe) or breaking (bump the major version). Removing a field or changing its type is a breaking change — even if nothing in myflames reads it.

4. **Self-describing.** Every field the schema exposes has a clear name, a documented type, and — for anything ambiguous — a short description either in a sibling `"$doc"` field or in the schema comment. "`rows`: 3000" is fine; "`cost`: 415.2" is not because the unit isn't obvious (is it ms? InnoDB's opaque cost number?) — specify.

5. **Human and machine must never drift.** If the HTML shows "Raise sort_buffer_size to 8M", the sidecar must have the same text in its `suggestions[]` array — the HTML reads it FROM the sidecar, not alongside it. One source, two projections.

6. **Greppable stability.** Field names use `snake_case`, enums use short stable strings (`"severity": "warn"`, not `"severity": "⚠️ warning!!"`), timestamps are ISO-8601 UTC. Agents depend on pattern-matching these values; don't decorate them.

## The Sidecar Schema (v1 — target shape)

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-04-10T15:22:01Z",
  "myflames_version": "1.2.0",
  "source": {
    "type": "file" | "live",
    "engine": "mysql" | "mariadb",
    "engine_version": "8.4.8",
    "fixture_path": "test/mysql-explain-hash-join.json"
  },
  "query": {
    "raw": "...",
    "beautified": "..."
  },
  "plan_summary": {
    "total_time_ms": 12.4,
    "rows_examined": 48000,
    "rows_sent": 5,
    "operator_count": 12,
    "max_depth": 5
  },
  "optimizer_switches": [
    {
      "name": "hash_join",
      "value": "on",
      "explanation": "Builds an in-memory hash table ...",
      "node_labels": ["Inner hash join"]
    }
  ],
  "warnings": [
    {
      "severity": "warn" | "info" | "error",
      "category": "full_scan" | "filesort" | "temp_table" | "hash_join" | "bnl" | "env",
      "text": "Filesort detected but sort_buffer_size is only 256 KB — ...",
      "node_labels": ["Sort"],
      "source": "plan" | "environment"
    }
  ],
  "suggestions": [
    {
      "severity": "high" | "medium" | "low",
      "category": "tuning_variable" | "index" | "optimizer_switch" | "engine",
      "action": "SET SESSION sort_buffer_size = 8*1024*1024;",
      "why": "When the sort set does not fit ...",
      "target_variable": "sort_buffer_size",
      "target_table": null
    }
  ],
  "executive_summary": "This query scans 3000 users ...",
  "primary_action": { "ref": "suggestions[0]" },
  "collected": {
    "schema": { /* collectors output */ },
    "stats":  { /* collectors output */ },
    "variables": { /* collectors output */ }
  }
}
```

## Process

1. **Pick the channel.** For each new data point, decide: sidecar only, sidecar + HTML, or HTML only. Default to sidecar + HTML mirror. Never emit data that's only in the SVG.

2. **Match an existing field, don't add one.** Before adding a new field, check whether an existing field can carry the data (maybe with an additional enum value). New top-level fields force schema version bumps.

3. **Write the schema first, the code second.** Sketch what the sidecar should look like for the new case, review it for stability and greppability, THEN write the code that produces it. Fixing a wrong schema after it ships is the worst case.

4. **Roundtrip test it.** For every new field, write a test that (a) generates a sidecar with the field populated, (b) re-parses it, (c) asserts semantic equality. If the field can round-trip, it's wire-stable.

5. **Validate against the schema on write.** Before the generator writes a sidecar, it should validate its own output (at minimum: required keys present, types match, enums are in the allowed set). Corrupt sidecars corrupt downstream agents silently — fail fast at emit time.

## Conventions

- **File naming:** `<base>.json` next to `<base>.svg` and `<base>.html`. Never `<base>-sidecar.json` or `<base>.analysis.json` — one name, one lookup rule.
- **Size budgets:** Sidecar should stay under ~64 KB for typical plans (so agents can load it without streaming). If it exceeds that, move `collected.schema` and `collected.stats` into secondary files linked by `$ref`.
- **No nulls for absent data:** Use the absence of a key, not `null`. `null` means "explicitly nothing there", not "we didn't look". This distinction matters for agents.
- **Enum discipline:** Every string that could be an enum is an enum — `severity`, `category`, `source`, `engine`, `schema_version`. Freeform strings are reserved for `text`, `why`, `action`, `explanation`, `executive_summary`.
- **Anchors for crossref:** When a warning points at a plan node, use a stable label in `node_labels[]` (matching `short_label` in the tree) so the HTML/SVG can highlight it and an agent can reason about which operator is affected.
- **JSON-LD embedding (fallback):** When HTML must be self-contained (single-file share), embed `<script type="application/ld+json">` in `<head>` with the same schema. Same schema version, same shape — no drift.

## Out of Scope

- MySQL correctness of the data being serialized (see `mysql-expert`).
- Visual layout of the HTML consuming the sidecar (see `progressive-ux`, `web-design`).
- Writing the SVG itself (see `viz-specialist`).
- Unit tests of the serializer (see `test-pro` — this skill supplies the schema contract, test-pro writes the assertions).

## Key Files in myflames

- [myflames/parser.py](myflames/parser.py) — `analyze_plan()` returns the dict that becomes the sidecar body. Any new field starts here.
- [myflames/advisor.py](myflames/advisor.py) — populates `environment_warnings` / `environment_suggestions` / `collected_*`. Same dict.
- [myflames/cli.py](myflames/cli.py) — currently renders SVG/HTML only. Needs an output_sidecar.py sibling that serializes the analysis dict to a stable JSON schema.
- [docs/demos/](docs/demos/) — every SVG there should eventually have a `.json` sibling.
