# MyFlames Project Rules

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview
**myflames** visualizes MySQL and MariaDB `EXPLAIN ANALYZE FORMAT=JSON` output as interactive SVG flame graphs, bar charts, treemaps, and Visual Explain–style diagrams. It requires MySQL 8.4+ or MariaDB 10.11+/11.4+ and Python 3.7+ with no external dependencies.

Inspired by [Brendan Gregg's FlameGraph](https://github.com/brendangregg/FlameGraph) and [Tanel Poder's SQL Plan FlameGraphs](https://tanelpoder.com/posts/visualizing-sql-plan-execution-time-with-flamegraphs/). **Never remove this inspiration / credit line** from the README (or anywhere else it appears). Brendan Gregg and Tanel Poder are the origin of this work and must always be acknowledged.

## 🛠 Skills & Specialized Agents
- **Testing Specialist (`/test-pro`)**: Located at `.claude/skills/test-pro/SKILL.md`. Use for writing, running, and fixing Python tests.
- **Visualization Specialist (`/viz-specialist`)**: Located at `.claude/skills/viz-specialist/SKILL.md`. Use for improving UI/UX, CSS, and SVG layouts in `docs/demos/`.
- **Web Dev (`/web-dev`)**: Located at `.claude/skills/web-dev/SKILL.md`. Use when building or modifying React/TypeScript/Vite/Tailwind components, pages, or API clients.
- **Web Design (`/web-design`)**: Located at `.claude/skills/web-design/SKILL.md`. Use when designing layouts, typography, color, spacing, or accessibility.
- **MySQL Expert (`/mysql-expert`)**: Located at `.claude/skills/mysql-expert/SKILL.md`. The MySQL/MariaDB brain — owns correctness of advisor rules, optimizer_switch explanations, and every plain-English claim myflames makes about a plan. Consult before adding or modifying `advisor.py` rules or `OPTIMIZER_SWITCH_EXPLANATIONS`.
- **Progressive UX (`/progressive-ux`)**: Located at `.claude/skills/progressive-ux/SKILL.md`. Designs outputs that serve newcomers AND senior DBAs via progressive disclosure (glossary chips, primary-action strips, collapsible details). Use when changing HTML wrappers, onboarding copy, or glossary entries.
- **Structured Output (`/structured-output`)**: Located at `.claude/skills/structured-output/SKILL.md`. Owns the JSON sidecar schema and every decision about what gets serialized where. Consult when adding new data to an output or changing the shape of what myflames emits for external tools / AI agents.

## 🧪 Development Workflow & Testing
- **Mandatory Tests**: Every new feature or logic change in the Python package MUST include a corresponding test case.
- **Verification**: Use the `/test-pro` skill to ensure coverage and verify that MySQL explain data is parsed correctly before completing a task.
- **Test Commands**:
  - Full suite: `./run-tests.sh`
  - Direct: `python3 -m unittest discover -s test -p "test_myflames.py" -v`

## 🎨 Visualization & UI Standards
- **Consistency**: All HTML demos and SVGs must share a unified design language (modern system fonts like Inter/Roboto, consistent color palettes for 'Join' vs 'Scan').
- **Modernization**: For any file in `docs/demos/`, ensure high-contrast colors and responsive layouts.
- **Review**: Proactively use the `/viz-specialist` skill when modifying renderers (`output_*.py`) to audit visual hierarchy and accessibility.

## 🏗 Architecture (Python package: `myflames/`)
1. **`parser.py`**: Single entry point. Builds a unified tree. Auto-detects MySQL vs MariaDB format. MariaDB normalization (`_normalize_mariadb*` functions) converts MariaDB's `query_block/nested_loop/table` structure into MySQL's `operation/inputs` tree before `parse_node` processes it. `analyze_plan(root)` scans for full_scans, hash_joins, etc.
2. **`cli.py`**: Argument parsing and SVG height patching for the analysis panel.
3. **Renderers**: `flamegraph.py`, `output_bargraph.py`, `output_treemap.py`, `output_diagram.py`.

## ⚠️ Key Constraints & Coding Standards
- **No External Dependencies**: Stdlib only. Python 3.7+ compatible (no walrus operator, no `match` statements).
- **Single Parser**: Never parse JSON in output modules; always use `parser.parse_explain`.
- **SVG Rules**: Always update both `height` AND `viewBox` together. Use `_wrap()` for info panel text.
- **Preserve Order**: Do not sort `inputs[]` in the JSON; order is semantically significant (outer vs inner table).
- **Preserve Inspiration Credit**: Never remove the "Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs" line from the README or anywhere else. This attribution is load-bearing for the project and must always remain.

## 📖 Commands Reference
- **Run**: `python3 -m myflames --type [flamegraph|bargraph|treemap|diagram] explain.json > output.svg`
- **MySQL Fixtures**: `./scripts/generate-fixtures.sh` (requires Docker) to regenerate MySQL `test/fixtures/`.
- **MariaDB Fixtures**: `./scripts/generate-mariadb-fixtures.sh` (requires Docker) to regenerate MariaDB `test/fixtures/`.
