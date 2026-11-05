# Command-line reference

The Python CLI is a first-class interface for offline plans, live inspection, reports and automation.

The CLI requires Python 3.7 or later and uses the Python standard library only.

```bash
pipx install myflames
# or: pip install myflames
```

From a source checkout, run `python3 myflames.py` or `python3 -m myflames` directly.

## Practical examples

Live examples assume saved `prod` and `dev` profiles and an `orders` table. `before.json` and `after.json` are captures from before and after a change; `explain.json` below can be either capture or an exported plan.

Capture a live plan with schema, table statistics and session variables, then render a self-contained HTML report:

```bash
myflames capture -p --profile prod -e "SELECT * FROM orders WHERE customer_id = 42" -o before.json
myflames --type workbench before.json -o plan.html
```

Compare two plans in an HTML diff or a compact text diff:

```bash
myflames compare before.json after.json -o diff.html
myflames compare before.json after.json --digest
```

Run a full server analysis report or list the heaviest statements from Performance Schema:

```bash
myflames report -p --profile prod --html -o server-report.html
myflames top -p --profile prod --order total --limit 25
```

Save a database's schema as a version, then compare that version with the database later, or compare the same database on two servers. The first side is the source (the schema to match) and the second the target: the sync script changes the target to match the source, and it is printed, never run:

```bash
myflames schema save -p --profile prod -D shop --name "before release 42"
myflames schema compare latest live -p --profile prod -D shop
myflames schema compare profile:prod/shop profile:staging/shop --sql -o sync-staging.sql
myflames schema lint live -p --profile prod -D shop
myflames schema export latest --sql --create-database -o shop.sql   # a script that recreates the schema; never run
```

Sum a slow query log by statement fingerprint (a file from MySQL 5.7 to 9.x, Percona Server or MariaDB, or `mysql.slow_log` with `--table`), optionally within a time range, with no server needed for a file:

```bash
myflames slow-log /var/log/mysql/slow.log --from '2026-10-31 09:00' --to '2026-10-31 10:00'
myflames slow-log --table --profile prod --order avg --limit 10
```

Dump only the definitions, without rows:

```bash
myflames dump backup-structure --no-data --schema shop --profile prod
```

The sync script leaves out objects that exist only in the target; add `--include-drops` to drop them, or name the objects to include with `--only KEY` (the `key` of each object in `--json`). Each `ALTER TABLE` in it carries an `-- ALGORITHM:` comment with the predicted INSTANT, INPLACE or COPY algorithm and whether writes wait, from the target's saved definition; no ALGORITHM or LOCK clause is added.

Compare global parameters or accounts between two servers, keep a parameter snapshot to compare later, and export grants as SQL without passwords (nothing is run):

```bash
myflames config snapshot save -p --profile prod --label "before upgrade"
myflames config compare profile:prod profile:staging
myflames config compare latest profile:prod
myflames accounts compare profile:prod profile:staging
myflames accounts export profile:prod --account 'app'@'10.0.%' -o grants.sql
```

Compare the parameter catalogs of two server releases, or trace one parameter through every cataloged release. The catalogs ship with myflames and no server is read:

```bash
myflames config versions list
myflames config versions compare 8.0.36 8.4.3
myflames config versions history innodb_io_capacity
```

Reach a server through an SSH tunnel with your own SSH keys or agent:

```bash
myflames profiles set prod --ssh bastion --ssh-user deploy
```

Hide literal values before you share a plan:

```bash
myflames explain.json -o shared.html --hide-literals
```

Run SQL from the command line under the connection's write-safety policy:

```bash
myflames sql -p --profile dev -e "SELECT id, status, total FROM orders ORDER BY id DESC LIMIT 10"
```

An `ALTER TABLE` asks first and prints `ALTER predicted:` with the algorithm the server is expected to use before you confirm.

Gate a CI pipeline on plan findings or server health checks (exits `1` when triggered):

```bash
myflames check after.json --fail-on full_scan,filesort,temp_table
myflames health -p --profile prod --fail-on problem
```

Start the read-only stdio MCP server for Claude Code, Cursor or another MCP host:

```bash
pip install 'myflames[mcp]'
myflames-mcp
```

```json
{
  "mcpServers": {
    "myflames": {
      "command": "myflames-mcp"
    }
  }
}
```

### Commands

Invoke subcommands with `myflames <command>` or `python3 -m myflames <command>`. The root command renders a plan file or stdin; its flags are listed below.

| Command | Primary options | Purpose |
| --- | --- | --- |
| `ui` | `[--port PORT] [--no-browser] [--no-restore]` | Deprecated: launch the browser performance workspace (use the macOS app, `myflames.app`). |
| `sql` | `[-e SQL \| -f FILE] [--format {table,tsv,csv,json,markdown,insert}] [--insert-table NAME] [--limit N] [-o PATH] [--yes]` | Run SQL in one editor session with safety policies. Results carry no column types: JSON keeps numbers as strings, table quotes them and INSERT writes them as quoted text. NULL and empty strings stay distinct: JSON uses `null` and `""`; CSV uses an unquoted empty field and `""`; table uses `NULL` and `""`; Markdown uses `*NULL*` and an empty cell. An ALTER TABLE confirmation adds an `ALTER predicted:` line (INSTANT, INPLACE or COPY, and whether writes wait). Interactive sessions are not supported. |
| `capture` | `[-e SQL \| --statement-digest HASH] [--mode {explain,analyze}] [--trace] [-o PATH]` | Capture estimated plans by default, with schema, table statistics and session variables. `--mode analyze` executes a SELECT in a rolled-back read-only transaction; `--trace` adds optimizer trace. `--digest HASH` is a deprecated alias of `--statement-digest HASH`. |
| `explore` | `PLAN [--type TYPE] [--metric METRIC] [--focus ID] [--hide-literals] [-o PATH]` | Export an interactive plan visualization HTML file. `--hide-literals` replaces literal values with `?`. |
| `compare` | `before.json after.json [--digest \| --json] [--hide-literals] [-o PATH]` | Compare two execution plans and report operator differences. `--hide-literals` replaces literal values with `?` in the HTML and JSON. |
| `diff` | `before.json after.json [--digest \| --json] [--hide-literals] [-o PATH]` | Deprecated alias of `compare`. |
| `top` | `[--order {total,time,avg,examined,rows,count,exec_count,full_join,scan,scans,no_index,tmp_tables,locks}] [--table SCHEMA.TABLE] [--schema NAME] [--limit N] [--json] [-o PATH] [-q]` | Rank expensive statements from Performance Schema digests. |
| `sessions` | `[--all] [--min-seconds N] [--session-user USER] [--schema NAME] [--kill ID [--query] [--yes]] [--explain ID]` | The CLI for the **Processlist** view: inspect connections, running queries, idle transactions and thread states, and the progress of running DDL (stage, percentage, estimated time left; `ddl` in `--json`). Kill protects Group Replication streams even when their command name is truncated. |
| `running` | `[--min-seconds N] [--explain ID] [--json] [-o PATH]` | Deprecated alias of `sessions`, retaining the `running-1.0` contract. |
| `locks` | `[--waits-only \| --deadlock \| --kill ID [--query] [--yes]]` | Trace lock waits, metadata blockers and deadlocks. `--kill ID` ends the connection; `--query` stops only its query. Kill shares the Group Replication stream guard with Sessions and reports when completion could not be verified. |
| `health` | `[--interval N] [--fail-on {problem,watch}]` | Sample server status counters over time to detect health anomalies. |
| `innodb` | `[--raw] [--fail-on {problem,watch}]` | Parse and summarize SHOW ENGINE INNODB STATUS output. |
| `error-log` | `[--level LEVEL[,LEVEL...]] [--limit N] [--fail-on {problem,watch}]` | Read the server error log: `performance_schema.error_log` on MySQL; on MariaDB the log file, only when the server runs on this computer. |
| `waits` | `[--interval N] [--limit N]` | Rank wait events over a sampling window and since server boot. |
| `indexes` | `[--schema NAME] [--all-schemas] [--limit N]` | Find unused and redundant indexes and tables read mostly by full scans; partitioned or unknown candidates stay under No data. |
| `tables` | `[--schema NAME] [--all-schemas] [--fresh]` | Find tables without a primary key or usable unique key, and report InnoDB reusable free space. Reusable allocation is informational and does not trigger a warning exit code on its own. |
| `io` | `[--interval N] [--schema NAME] [--all-instances]` | Analyze file and table I/O throughput. |
| `schema` | `read`, `save [--name NAME] [--note TEXT]`, `list [--database NAME]`, `show VERSION [--object NAME]`, `compare SOURCE TARGET [--ignore-definers] [--include-drops \| --only KEY] [--unchanged] [--fail-on {any,drops}] [--json \| --markdown \| --sql]`, `lint SIDE [--fail-on watch] [--json]`, `edit VERSION`, `delete VERSION [--yes]`, `export VERSION [--sql [--create-database] [--keep-definers] [--target-database NAME]]`, `import PATH` | Save a database's schema as a local version (`schema-snapshot-1.0`), list and export versions, compare two schemas (`schema-compare-1.0`) and check one for risks (`schema-lint-1.0`). A side is a saved version (ID, ID prefix or name), `latest` (the newest version of the other side's database), `live` (the connection flags and `-D`), `profile:NAME[/DATABASE]` (a saved connection with its saved password) or `file:PATH` (a `.json` path also works). `--sql` prints the sync script that would change TARGET to match SOURCE; it is never run. It leaves out objects only in the target unless `--include-drops` includes them, or repeatable `--only KEY` (`namespace:name`) chooses exactly what to include. `lint` reports Watch findings and Not judged checks with their reasons. `export VERSION --sql` prints a script that recreates the schema on another server (VERSION may also be `live`, `profile:NAME[/DATABASE]` or `file:PATH`): tables in foreign key order, views in dependency order, routines, triggers and events in `DELIMITER` blocks, definers left out unless `--keep-definers`, `--create-database` starts with `CREATE DATABASE`, `--target-database NAME` writes it for another database name; it is never run. Read-only on servers. |
| `slow-log` | `[FILE] [--table] [--from TIME] [--to TIME] [--executed-by USER] [--schema NAME] [--order {total,count,avg,max,p95,lock,examined,sent}] [--limit N] [--json] [-o PATH] [-q]` | Sum a slow query log (a file, `-` for standard input, or `mysql.slow_log` with `--table` and the connection flags) by statement fingerprint: count, total, average, P95 and maximum time, lock time and rows, within an optional time range in the log's own clock (`slow-log-1.0`). Read-only. |
| `config` | `[--fail-on LEVEL]`; `set NAME VALUE [--persist \| --persist-only \| --session] [--yes]`; `reset-persist NAME [--yes]`; `snapshot save [--label TEXT]`, `snapshot list`, `snapshot show SNAPSHOT`, `snapshot delete SNAPSHOT [--yes]`; `compare SOURCE TARGET [--all] [--fail-on different] [--json]` | Review server settings or change one variable after exact SQL review. No modifier on `set` means global. `snapshot` keeps global parameters on this computer (`parameter-snapshot-1.0`); `compare` compares them between two sides without changing either server (`parameter-compare-1.0`). A side is `live`, `profile:NAME`, `snapshot:ID`, `latest` (the newest snapshot of the other side's server) or `file:PATH`. `--all` adds identical and identity (server-specific) parameters to the text. `versions list`, `versions compare FROM TO [--engine E] [--kind KIND] [--name TEXT] [--json \| --markdown]` and `versions history NAME [--engine E] [--json \| --markdown]` answer from the shipped release catalogs (`parameter-version-catalog-1.0`, `parameter-version-compare-1.0`, `parameter-history-1.0`) and read no server; compare infers the engine when only one catalog has both versions, and history without `--engine` prints every engine that has the name. |
| `instruments`| `[--preset {minimal,locks,waits,defaults}]` | Review Performance Schema instrumentation and generate setup SQL. |
| `report` | `[--interval N] [--schema NAME] [--all-schemas] [--html \| --json \| --markdown] [-o PATH]` | Compose the Full analysis report from health, waits, I/O, configuration, InnoDB, sessions, instruments, server checks, tables and indexes. Error log and Locks are outside its coverage. |
| `replication` | `[--channel NAME]`; `skip --channel NAME [--yes]`; `skip-loop --channel NAME --errors CODES [--max N] [--max-minutes M] [--yes]` | Monitor replica lag and source versions, skip one failed transaction, or run a bounded recovery loop. Both recovery commands require `--channel`; use an empty string for the default channel. |
| `cluster` | `[--fail-on {problem,watch}] [--json] [-o PATH] [-q]` | Show the Galera, Percona XtraDB Cluster, InnoDB Cluster or Group Replication cluster the server belongs to (`cluster-1.0`); read-only. |
| `accounts` | `list`, `show`, `create`, `drop`, `rename`, `lock`, `unlock`, `expire`, `password`, `grant`, `revoke`, `role grant`, `role revoke`, `role default`; `compare SOURCE TARGET [--fail-on different] [--json]`; `export SIDE [--account USER@HOST]` | Inspect and manage users, roles, passwords and privileges. Writes require review and support `--yes` and `--json`. `compare` compares users and roles between two sides (`live` or `profile:NAME`) without changing either (`account-compare-1.0`). `export` prints the chosen accounts and the roles they need as SQL without passwords; it is never run, and built-in system accounts are left out unless named. |
| `privileges` | `[--json]` | Report effective permissions and per-action verification status. |
| `upgrade-check`| `--target-version X.Y.Z [--no-shell]` | Run MySQL Shell upgrade checks or native compatibility checks. |
| `dump` | `DIRECTORY [--schema NAME] [--table NAME] [--no-data] [--threads N] [--yes] [--preflight]` | Dump an instance, schemas or tables to a new or empty directory with preflight checks and checksum manifests. Repeat `--schema` or `--table` for several names. `--no-data` dumps definitions only (MySQL Shell `ddlOnly`, `mysqldump` and `mariadb-dump` `--no-data`); the manifest records it and a restore then skips the row count check. |
| `restore` | `DIRECTORY [--confirm-target TARGET] [--yes] [--preflight]` | Restore a validated dump and verify objects and recorded row counts (a structure-only dump skips the row counts). Confirmation must match the complete target printed by the plan, including a schema or directory when present. |
| `clone` | `--donor-profile NAME [--data-directory PATH] [--confirm-target TARGET] [--yes] [--preflight]` | Clone a MySQL donor into the connected recipient with `CLONE INSTANCE` after a preflight of both servers, in place or into `--data-directory`. Alternatively select the donor with `--donor-host HOST --donor-port PORT --donor-user USER --donor-password` (prompts for its password). Confirm the complete target printed by the plan. |
| `profiles` | `list`, `set NAME [--type TYPE] [--read-only \| --read-write] [--ssh HOST [--ssh-user USER] [--ssh-port PORT] \| --no-ssh]`, `forget NAME`, `delete NAME` | Manage saved connections and safety settings; `forget` removes a saved password. `--ssh` connects through an SSH tunnel with your own `~/.ssh/config`, keys and agent (no SSH passwords); changing the tunnel forgets the saved password. |
| `audit` | `list [--since DAYS] [--server NAME] [--limit N]`, `export [--json \| --csv]`, `purge [--older-than DAYS]` | Query, export and maintain the local server write audit trail. |
| `check` | `PLAN [--fail-on LIST] [--ignore LIST]`; `--list-rules` | Gate plans on categories such as `full_scan`, `filesort`, `temp_table` and `misestimate`, severities, or `any`. Use `--list-rules` for the complete catalog. |
| `advise` | `PLAN [--ignore LIST] [--json]` | Provide ranked optimization suggestions for an execution plan. |
| `findings` | `PLAN [--ignore LIST] [--json] [-o PATH]` | Deprecated alias of `advise`. |
| `digest` | `PLAN [--cost] [--tokenizer {heuristic,claude,gpt}] [--json]` | Emit a compact text digest. `--cost` compares token costs; `--json` emits that cost comparison, not the digest text. |
| `tokens` | `PLAN [--cost] [--json] [--show-prompts] [-o PATH]` | Deprecated alias of `digest`, defaulting to `--cost`. |
| `import-shell` | `BUNDLE [--query N] [--list] [-o PATH] [-q]` | Convert a diagnostics ZIP produced by MySQL Shell `util.debug.collectSlowQueryDiagnostics()` into capture JSON. |
| `guide` | `[-o PATH] [-q]` | Show which visualization to choose for a use case. |
| `teach` | `[LESSON] [--index \| --catalog] [--title TITLE] [-o PATH] [-q]` | Render a lesson such as `btree`, `bnl`, `hash`, `join` or `lru`, or use `--index` or `--catalog` for the catalog. Run `teach --help` for every lesson name. |

### Agent and CI subcommands

```bash
# Compact text digest and token cost comparison
myflames digest explain.json
myflames digest explain.json --cost --tokenizer claude

# Ranked findings as a JSON array
myflames advise explain.json --json

# Before/after plan comparison for pull requests or agents (compare-1.0)
myflames compare before.json after.json --digest
myflames compare before.json after.json --json

# CI gate: exit 0 when clean, 1 when a listed rule category fires, 2 on bad input
myflames check explain.json --fail-on full_scan,filesort,temp_table
myflames check --list-rules

# Schema drift gate: exit 1 when the database differs from its newest saved version
myflames schema compare latest live --profile prod -D shop --fail-on any -q

# Schema checks gate: exit 1 on a Watch finding (Not judged never trips it)
myflames schema lint live --profile prod -D shop --fail-on watch --json

# Export a saved schema version as a SQL script (never run by myflames)
myflames schema export latest --sql --create-database -o shop.sql

# Slow log by fingerprint in a time range (file, no server), or mysql.slow_log
myflames slow-log /var/log/mysql/slow.log --from '2026-10-31 09:00' --json

# Structure-only dump
myflames dump backup-structure --no-data --schema shop --profile prod

# Parameter and account drift between two servers: exit 1 when anything differs
myflames config compare profile:prod profile:staging --fail-on different --json
myflames accounts compare profile:prod profile:staging --fail-on different -q

# Parameter changes between two releases, or one parameter across releases (offline)
myflames config versions compare 8.0.36 8.4.3 --kind default --json
myflames config versions history innodb_io_capacity --markdown

# Route a saved connection through an SSH tunnel (keys or agent only)
myflames profiles set prod --ssh bastion --ssh-user deploy --ssh-port 2222
myflames profiles set prod --no-ssh
```

Read and analysis gates generally use `0` for success, `1` for a triggered gate and `2` for bad input or connection failure. Write commands also distinguish unsent refusals from sent failures: started lifecycle jobs and variable changes can return `1` for failure, cancellation or an unverifiable result without `--fail-on`. See each command's help.

### Visualization options

| Flag | Default | Description |
| :--- | :--- | :--- | :--- |
| `--type` | `flamegraph` | Output chart type: `flamegraph`, `bargraph`, `diagram`, `tree`, `workbench` (`treemap` is a deprecated alias for `bargraph`). |
| `-o`, `--output PATH` | stdout | Write output to `PATH`. Use `.html` for a self-contained HTML report. |
| `--width PX` | `1800` / `1200` | SVG width in pixels (`1800` for flame graphs, `1200` for other views). |
| `--height PX` | `32` | Frame height in pixels for flame graphs. |
| `--colors SCHEME` | `hot` | Flame graph palette: `hot`, `mem`, `io`, `red`, `green`, `blue`. |
| `--title TEXT` | `MySQL Query Plan` | Custom chart title. |
| `--inverted` | off | Render an icicle graph (root at top). |
| `--no-enhance` | off | Disable detailed flame graph tooltips. |
| `--query SQL` | none | Embed SQL query text in the output report. |
| `--query-file PATH` | none | Embed SQL query text read from `PATH`. |
| `--hide-literals` | off | Replace literal values with `?` in shared plan HTML and JSON (also on `explore` and `compare`). Applied after a live capture, never to the SQL sent. |
| `--sidecar PATH` | auto with `-o` | Write the schema version `1.4` JSON sidecar to `PATH`. Both `plan.html` and `plan.svg` produce `plan.json` by default. |
| `--no-sidecar` | off | Do not write the automatic JSON sidecar beside the output file. |
| `--no-teach-bundle` | off | Do not emit the `teach/` lesson folder beside `.html` output. |
| `--refresh-teach-bundle` | off | Regenerate the `teach/` folder beside `.html` output even if it exists. |
| `-q`, `--quiet` | off | Suppress informational stderr messages while preserving stdout and exit codes. |

### Connection options

| Flag | Description |
| :--- | :--- | :--- |
| `--profile NAME_OR_ID` | Use a saved connection profile (and its saved password) for `-e` and live subcommands (`sql`, `capture`, `top`, `sessions`, `locks`, `innodb`, `error-log`, `health`, `waits`, `indexes`, `tables`, `io`, `config`, `instruments`, `report`, `replication`, `cluster`, `upgrade-check`, `dump`, `restore`, `clone`, `privileges`, `accounts`, `schema`). Explicit flags override profile fields. A profile set with `--ssh` connects through its SSH tunnel. |
| `-h`, `--host HOST` | Server hostname or IP address (default `127.0.0.1`; host-and-port connections use TCP). |
| `-P`, `--port PORT` | Server TCP port (default `3306`). |
| `-u`, `--user USER` | Database username. |
| `-p`, `--password [PASS]` | Prompt for a password on `/dev/tty` when passed without a value, or use an inline value (a warning is printed; prefer `-p` alone or `MYSQL_PWD`). |
| `-D`, `--database DB` | Default database name. |
| `--ssl-mode MODE` | TLS mode: `DISABLED`, `PREFERRED`, `REQUIRED`, `VERIFY_CA`, `VERIFY_IDENTITY`. |
| `--ssl-ca PATH` | CA certificate bundle path. |
| `--ssl-cert PATH` | Client TLS certificate path for root live rendering, `capture` and the Server view commands; not `sql` or lifecycle commands. |
| `--ssl-key PATH` | Client TLS private key path for root live rendering, `capture` and the Server view commands; not `sql` or lifecycle commands. |
| `--mysql-binary PATH` | Path to the `mysql` or `mariadb` client binary for root live rendering, `capture` and the Server view commands; not `sql` or lifecycle commands. |
| `-e`, `--execute SQL` | Run `EXPLAIN ANALYZE` on `SQL` against the live connection (or execute `SQL` in `myflames sql`). |
| `--no-collect-schema` | Skip `SHOW CREATE TABLE` collection during root `-e` only. |
| `--no-collect-stats` | Skip `information_schema.tables` collection during root `-e` only. |
| `--no-collect-variables` | Skip `SHOW SESSION VARIABLES` collection during root `-e` only. |
