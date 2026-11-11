# MyFlames handbook

The handbook is part of the MyFlames project and is the source of truth for the
user documentation displayed by the separate project website. Edit these
Markdown chapters here; do not maintain a second prose copy in the website.


## Start here

- [Installation & first steps](quickstart.md)
- [How-to guides](howto.md)
- [Why myflames](motivation.md)

## Workflows

- [Execution plans & comparison](plans.md)
- [Query editor & slow queries](queries.md)
- [Server diagnostics & operations](server.md)
- [Schemas & version comparison](schemas.md)
- [Learn the algorithms](learn.md)

## Reference

- [Connection policies & write safety](safety.md)
- [Engine & platform compatibility](compatibility.md)

## Community

- [Contribute to myflames](contributing.md)

## Maintaining the handbook

`manifest.json` defines chapter IDs, titles, groups, order and source files.
Keep the Markdown links relative so the chapters also work in this repository.
Feature contracts remain in [DEVELOPER_REFERENCE.md](../DEVELOPER_REFERENCE.md).
The handbook explains existing behavior; changes to behavior still need the
project's normal implementation, review and testing gates.

The separate website reads this manifest and these chapters when its
`build_docs.py --source /path/to/myflames` command runs. It generates browser
content and exact source downloads. The website does not write to this folder.
The current website identity, page layouts and release installers remain in
the website directory, outside this repository.
