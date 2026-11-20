# Contribute to myflames

Start with the [public repository](https://github.com/vgrippa/myflames), [open issues](https://github.com/vgrippa/myflames/issues) and [ROADMAP.md](../../ROADMAP.md). Check whether an idea fits the desktop workspace's direction before building a large change.

## Helpful contributions

- A reproducible bug report naming the operating system, database engine and exact version, steps, expected behavior and observed result.
- A sanitized `EXPLAIN` or `EXPLAIN ANALYZE` JSON fixture that captures an unsupported plan shape.
- A documentation or parameter-catalog correction backed by the engine source tree or official manual.
- A focused pull request with tests for changed logic and a matching user-facing entry under `[Unreleased]` in [CHANGELOG.md](../../CHANGELOG.md).

Inspect plans, logs and schemas for host names, account names and literal data before attaching them to a public issue.

## Development setup

The Python backend (`myflames/`) uses Python 3.7+ and only the standard library. The React and TypeScript UI lives in `apps/workspace`, and the Electron desktop shell lives in `apps/desktop` (Node.js 22.12 or newer):

```bash
git clone https://github.com/vgrippa/myflames.git
cd myflames
python3 -m pip install -e .
npm --prefix apps/workspace ci
npm --prefix apps/workspace run build
npm --prefix apps/desktop ci
npm --prefix apps/desktop start
```

Development runs store their data under `myflames-desktop-development`, separate from your installed app's workspace. See the [desktop reference](../DESKTOP.md) for packaging and platform details.

## Validate a change

Every new feature or logic change in `myflames/` requires tests. Run tests against an isolated temporary `HOME` and `USERPROFILE` with `MYFLAMES_SECRET_BACKEND=vault`; never run tests against a real saved workspace.

- `./run-tests.sh` runs the full Python test suite in parallel across isolated temporary homes (`JOBS=1` runs in a single process).
- `scripts/check_fast.sh [FILE...]` checks Python 3.7 syntax, tests for changed Python modules, TypeScript types (`tsc --noEmit`), shell scripts and workflow YAML before you hand a patch back.
- Changes that touch MySQL, MariaDB, ProxySQL, Readyset, replication or clusters must be verified against engine ground truth (`mysql-server`, `mariadb-server`, `mysql-shell` or the official manual) and validated against live servers, not mocks alone.
- UI changes must be checked in light and dark appearances at 1440 px and at the 720 px minimum window width.
- User-facing features and bug fixes update [README.md](../../README.md), [DEVELOPER_REFERENCE.md](../DEVELOPER_REFERENCE.md), [CHANGELOG.md](../../CHANGELOG.md) `[Unreleased]` and the matching chapter in `docs/handbook/` in the same pull request.

Contributions are licensed under Apache-2.0. Never copy code from `flamegraph.pl` (CDDL 1.0) or remove the Brendan Gregg and Tanel Poder inspiration credit. Read [CONTRIBUTING.md](../../CONTRIBUTING.md), the [developer reference](../DEVELOPER_REFERENCE.md) and [ROADMAP.md](../../ROADMAP.md) for full details.
