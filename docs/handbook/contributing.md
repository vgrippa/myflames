# Contribute to myflames

Start with the [public repository](https://github.com/vgrippa/myflames), [open issues](https://github.com/vgrippa/myflames/issues) and the project's roadmap. Check whether your idea fits the direction before implementing a large feature.

## Helpful contributions

- A reproducible bug report with engine and exact version, steps, expected behavior and observed result.
- A sanitized execution-plan fixture that captures an unsupported engine shape.
- A documentation correction with a link to the relevant source or manual.
- A focused patch with tests for changed logic and an explanation of the behavior.

Review a plan, log and schema for sensitive information before attaching them to a public issue.

## Development setup

```bash
git clone https://github.com/vgrippa/myflames.git
cd myflames
python3 -m pip install -e .
```

The Python package uses only the standard library. Frontend and desktop development have their own Node build toolchains. Read the current repository instructions before modifying either.

## Validate a change

New Python features and logic changes need corresponding tests. Run tests with an isolated temporary HOME and USERPROFILE and `MYFLAMES_SECRET_BACKEND=vault`; never test against your real saved workspace.

The repository's `run-tests.sh` is the full Python suite entry point. Features touching engine behavior require live-server validation, and UI changes require browser or packaged-app checks where appropriate. Follow the current AGENTS.md and contributing guide for the specific area.

Read [CONTRIBUTING.md](../../CONTRIBUTING.md), the [developer reference](../DEVELOPER_REFERENCE.md), and [ROADMAP.md](../../ROADMAP.md).
