# myflames for MySQL Shell

A MySQL Shell plugin that adds a `myflames` global object to `mysqlsh`. It runs
`EXPLAIN ANALYZE FORMAT=JSON` for a statement on your current Shell session and
renders the plan with the installed myflames package: a flame graph or Visual
Explain HTML report, a token-cheap digest for LLMs, or ranked advisor findings.

Inspired by Brendan Gregg's FlameGraph and Tanel Poder's SQL Plan FlameGraphs.

## Requirements

- MySQL Shell 8.0.22 or later (the Python plugin manager and `--pym` both
  shipped in 8.0.22). The SQL handler needs MySQL Shell 9.0.0 or later.
- MySQL Server 8.3 or later (`explain_json_format_version=2` and
  `EXPLAIN ANALYZE FORMAT=JSON`). MySQL 8.4 LTS or 9.x is recommended.
- MySQL only. MySQL Shell does not target MariaDB, and the plugin refuses
  MariaDB servers. Use the myflames CLI for MariaDB plans.

## Install

1. Install myflames into MySQL Shell's own Python (Shell embeds its own
   interpreter, so a `pip install` into your system Python is not visible to it):

   ```sh
   mysqlsh --pym pip install --user myflames
   ```

   Check it: `mysqlsh --py -e "import myflames, sys; print(myflames.__file__, sys.version)"`

2. Copy or symlink the plugin folder into your Shell plugins directory:

   ```sh
   mkdir -p ~/.mysqlsh/plugins
   ln -s "$(pwd)/contrib/mysqlsh-plugin/myflames" ~/.mysqlsh/plugins/myflames
   # or: cp -R contrib/mysqlsh-plugin/myflames ~/.mysqlsh/plugins/
   ```

   On Windows the plugins directory is `%AppData%\MySQL\mysqlsh\plugins`.

3. Restart `mysqlsh`. `\? myflames` shows the help.

The plugin folder intentionally has no `__init__.py`. MySQL Shell puts the
plugins directory first on `sys.path` while it loads `init.py`, so a regular
package named `myflames` there would shadow the real myflames package. If
`import myflames` fails, every function tells you the exact install command.

## Usage

JavaScript mode:

```js
\connect root@localhost:3306
myflames.explain("SELECT * FROM orders o JOIN users u ON u.id = o.user_id")
myflames.explain("SELECT ...", {type: "workbench", output: "/tmp/plan.html"})
myflames.explain("SELECT ...", {analyze: false, open: false})
myflames.digest("SELECT ...")
myflames.advise("SELECT ...")
```

Python mode:

```python
myflames.explain("SELECT * FROM orders o JOIN users u ON u.id = o.user_id")
myflames.explain("SELECT ...", {"type": "bargraph", "output": "/tmp/plan.svg"})
myflames.explain("SELECT ...", analyze=False)
myflames.digest("SELECT ...")
myflames.advise("SELECT ...")
```

### `myflames.explain(query[, options])`

Captures the plan and writes a report. Prints the output path.

| Option    | Default              | Meaning |
|-----------|----------------------|---------|
| `analyze` | `true`               | `EXPLAIN ANALYZE FORMAT=JSON` (executes the statement). `false` runs `EXPLAIN FORMAT=JSON` (estimates only). |
| `output`  | a new temporary `.html` | Output path. A path ending in `.svg` writes a bare SVG chart; anything else writes the self-contained HTML report. |
| `type`    | `flamegraph`         | `flamegraph`, `bargraph`, `tree` or `workbench` (Visual Explain). |
| `open`    | `true`               | Open the output in your default browser. |

### `myflames.digest(query[, options])` and `myflames.advise(query[, options])`

`digest` prints the compact plan digest (same as `myflames digest`), ready to
paste into an LLM prompt. `advise` prints ranked warnings and suggestions with
confidence (same layout as `myflames advise`). Both accept `analyze`.

### What runs on your session

For `analyze: true` the plugin sends, in order:

1. `SELECT VERSION()` (refuses MariaDB and MySQL older than 8.3)
2. a Performance Schema check for an open transaction in your session (it
   refuses rather than letting `START TRANSACTION` implicitly commit your work;
   skipped if Performance Schema is unavailable)
3. `SELECT @@SESSION.explain_json_format_version`, then
   `SET SESSION explain_json_format_version = 2` if it was not already 2
4. `START TRANSACTION READ ONLY`
5. `EXPLAIN ANALYZE FORMAT=JSON <your statement>`
6. `ROLLBACK` (always, also on error)
7. `SET SESSION explain_json_format_version = <previous value>` (always)

`EXPLAIN ANALYZE` executes the statement, so analyze mode only accepts
`SELECT`, `WITH ... SELECT`, `TABLE` and `VALUES` statements, and refuses
`SELECT ... INTO OUTFILE/DUMPFILE`. Use `{analyze: false}` for an estimated
plan of an `INSERT`/`UPDATE`/`DELETE`/`REPLACE`; that mode does not open a
transaction because plain `EXPLAIN` does not execute the statement.

## SQL handler (MySQL Shell 9.0+)

Enable it once per Shell session, for example from `~/.mysqlsh/mysqlshrc.py`
or interactively:

```js
myflames.enableSqlHandler()            // JS
myflames.enableSqlHandler({open: false, type: "workbench"})
```

```python
myflames.enable_sql_handler()          # Python
```

Then, in SQL mode (`\sql`):

```sql
EXPLAIN ANALYZE FORMAT=MYFLAMES SELECT * FROM orders WHERE total > 100;
EXPLAIN FORMAT=MYFLAMES SELECT * FROM orders WHERE total > 100;
```

`FORMAT=MYFLAMES` is not a MySQL format, so the handler never intercepts a
normal `EXPLAIN`, `EXPLAIN FORMAT=JSON` or `EXPLAIN FORMAT=TREE`. The statement
returns a one-row result with the report path. The same safety rules as
`myflames.explain()` apply.

`myflames.disableSqlHandler()` turns it off. MySQL Shell has no API to
unregister an SQL handler, so after disabling, `FORMAT=MYFLAMES` statements fail
with a message telling you how to enable the handler again. On MySQL Shell
versions older than 9.0.0 `enableSqlHandler()` prints the minimum version.

## Command line

`explain`, `digest` and `advise` are also available from the Shell command line:

```sh
mysqlsh root@localhost:3306 -- myflames explain "SELECT * FROM orders" --output=/tmp/plan.html --open=false
mysqlsh root@localhost:3306 -- myflames digest "SELECT * FROM orders"
mysqlsh root@localhost:3306 -- myflames advise "SELECT * FROM orders" --analyze=false
```

## Limitations

- MySQL only (8.3+). MariaDB servers are refused.
- `analyze: true` runs the statement for real, inside a READ ONLY transaction
  that is rolled back. A slow query is slow here too.
- The open transaction check is best effort. Run the plugin outside an explicit
  transaction.
- The HTML report links to myflames teach lessons in `./teach/`, which the
  plugin does not write next to the report. Use the myflames CLI
  (`myflames --output report.html`) if you need the bundle.
- Session temporary tables and user variables work because the plugin uses
  your current session.
