# MyFlames Gemini Rules
Project rules are in AGENTS.md. Read that file whether or not this tool expands the import below.
@AGENTS.md

---

## Strict Reliability & Verification Directives

These directives are mandatory for all tasks in this repository. Reliability is top priority.

### 1. Absolute Prohibition on Mock-Only Validation
- **Never rely exclusively on unit-test mocks** for database engines, ProxySQL, replication, cluster operations, or network endpoints.
- Mocks only prove code agrees with its author's assumptions.
- Any feature touching MySQL, MariaDB, ProxySQL, or cluster management must be validated live against real daemons (e.g. Docker containers via `scripts/cluster-matrix.sh` or `scripts/ga-matrix.sh`) before declaring completion.
- Never mock out or bypass `dbops.py` classification, token generation, or policy enforcement in tests.

### 2. Engine Ground-Truth Rule (C++ Source Verification)
- The adjacent repositories `mysql-server` and `mariadb-server` are present in this environment (`../mysql-server`, `../mariadb-server`).
- Never guess system variable names, UDF arguments, column names, or cluster command syntax (e.g., MySQL Group Replication switchover requires `MEMBER_ID` UUID, not hostname).
- Check the engine source code or official technical documentation before writing database control statements.

### 3. Write-Safety Contract & Cryptographic Review Invariant
- **Exact Reviewed SQL == Exact Executed SQL**: The exact SQL string reviewed and approved by the user in the UI must be cryptographically bound in a review token (`reviewed_writes` / `dbops`).
- Action endpoints must **strictly require** the review token, reject unreviewed execution, and NEVER dynamically re-plan or mutate statements at execution time.
- Action classification must strictly use valid `dbops.KINDS` (`read`, `write`, `risky`). Introducing unsupported kinds (e.g. `dml`) is prohibited.
- Check target state right before executing to detect plan drift (TOCTOU prevention).

### 4. Zero Collateral UI Regressions
- Never remove, unwrap, or alter top-level structural layout wrappers (e.g. `.topbar-stack`, `.main-layout`) when modifying child components or toolbars.
- Always verify sticky headers, scroll containers, responsive breakpoints, and sidebar visibility before considering UI work complete.
- Verify through headless browser probes or screenshots (`scripts/screenshot.mjs`).

### 5. Multi-Perspective Adversarial Pre-Flight Gate
Before declaring any task done, reporting success, or launching the desktop app, you MUST systematically execute and pass 4 explicit audits:
1. **Write Safety & Permissions Audit**: Verify that no unreviewed write or token bypass is possible.
2. **Engine Correctness Audit**: Verify statements, parameters, and variable names against official engine specifications.
3. **Live Daemon Smoke Test**: Verify HTTP endpoints return `200` with parsed data against live running instances.
4. **Visual & Layout Regression Test**: Verify page scrolling, sticky positioning, and navigation across viewports.
