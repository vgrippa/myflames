# CI & agents

Use the CLI for repeatable checks and the optional MCP entry point for agent access. Neither requires reading a screenshot of the plan.

## Compact plan output

```bash
myflames digest explain.json
myflames compare before.json after.json --digest
```

The digest provides a compact textual representation for tools and review. JSON output exposes structured contracts. Use `--help` on a command to check its flags and exit behavior before wiring it into a pipeline.

## Plan checks in CI

`myflames check` applies plan rules and can return a failing exit code when the configured findings are present. Select rules that match your workload. A rule such as full scan does not by itself prove a performance defect: small tables and analytical queries may intentionally scan.

See [Agent and CI subcommands](cli.md#agent-and-ci-subcommands) for the exact current flags and supported rules.

## MCP tools

The optional `myflames-mcp` entry point exposes analysis and read-only server inspection. Live tools require a saved profile. Compact text is the default; `detail="full"` returns module JSON where supported.

Examples of inspection tools include `server_report`, `top_statements`, `sessions`, `locks_report`, `health_sample`, `innodb_status`, `my_privileges`, `replication_status` and `cluster_status`. These inspection tools have no password or write arguments.

Use your MCP client's documented stdio configuration to invoke `myflames-mcp`. Store and review the profile locally, rather than embedding a database password in a shared client configuration.

The [developer reference](../DEVELOPER_REFERENCE.md#mcp-server) defines tool schemas and expected failures.
