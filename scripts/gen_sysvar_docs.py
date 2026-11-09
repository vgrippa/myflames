#!/usr/bin/env python3
"""Write myflames/sysvar_docs.py: where the official manuals document each
system variable, so Server > Parameters can link to them.

This script never opens a network connection. A maintainer saves the official
reference pages first and passes their paths:

* MySQL: one "Server Option, System Variable, and Status Variable Reference"
  page per manual (https://dev.mysql.com/doc/refman/<v>/en/server-option-variable-reference.html),
  with --mysql <v>=<file>. Its system variable links name the page and the
  #sysvar_<name> anchor. --mysql-pages <v>=<dir> (the linked pages, saved
  under their file names) drops any anchor those pages do not define.
* MariaDB: the "Full List of MariaDB Options, System and Status Variables"
  page (https://mariadb.com/docs/server/server-management/variables-and-modes/full-list-of-mariadb-options-system-and-status-variables),
  with --mariadb <file>. Only lowercase system variable names whose link text
  equals the anchor are kept; status variable pages are skipped. --mariadb-pages
  <dir> holds the linked pages saved under their URL paths (dir/docs/....html)
  and drops anchors they do not define.
* Percona Server: a saved copy of the manual (https://docs.percona.com/percona-server/<v>/,
  every page in its sitemap.xml, saved under its URL path) with --percona
  <v>=<dir>. A heading whose text is a variable name and whose section has
  the option table (Scope, Dynamic or Command-line) documents that variable.
* Percona XtraDB Cluster: the same with --pxc <v>=<dir> for its manual
  (https://docs.percona.com/percona-xtradb-cluster/<v>/; wsrep-system-index.html
  holds its variables).

Example:
  python3 scripts/gen_sysvar_docs.py --saved-on 2026-10-21 \\
      --mysql 8.4=ref-8.4.html --mysql-pages 8.4=refman-8.4/ \\
      --mariadb full-list.html --mariadb-pages mariadb/ \\
      --percona 8.4=percona-8.4/ --pxc 8.4=pxc-8.4/ -o myflames/sysvar_docs.py
"""
import argparse
import html
import os
import re
from pathlib import Path

# One reference row: the linked name, then Cmd-Line, Option File, System Var, Status Var, Var Scope, Dynamic.
_MYSQL_ROW = re.compile(r'<tr><th scope="row"><a[^>]*href="([a-z0-9-]+)\.html#([A-Za-z0-9_.-]+)"[^>]*>([^<]*)</a>'
                        r'[^<]*</th>(.*?)</tr>', re.S)
_MARIADB_LINK = re.compile(r'<a[^>]*href="/docs/([^"#]+)(?:#([^"]*))?"[^>]*>(.*?)</a>', re.S)
_HEADING = re.compile(r'<h([1-6]) id="([^"]+)"[^>]*>(.*?)</h\1>', re.S)
# An anchor is an element id or, in the MySQL manual, an <a name="...">.
_ID = re.compile(r'\b(?:id|name)="([^"]+)"')
_NAME = re.compile(r'[a-z][a-z0-9_.]*')


def _text(fragment):
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).replace("¶", "").strip()


def _version(value):
    if not re.fullmatch(r"\d+\.\d+", value):
        raise SystemExit("A manual version is major.minor, such as 8.4: %r" % value)
    return value


def _pair(value):
    version, _, path = value.partition("=")
    if not path:
        raise SystemExit("Use VERSION=PATH: %r" % value)
    return _version(version), path


def _ids(path):
    try:
        return set(_ID.findall(Path(path).read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return None


def mysql_links(text, pages_dir=None):
    """{name: (page, anchor)} for each row of a MySQL option and variable
    reference whose System Var column says Yes.

    Most rows link to page#sysvar_<name>; some link to an option anchor
    (sql_mode to server-options.html#option_mysqld_sql-mode) although a
    sysvar_<name> anchor exists on another page. With *pages_dir* (every
    linked page saved under its file name) the sysvar_<name> anchor is used
    wherever a saved page defines it, then the linked anchor if its page
    defines it; anything else is dropped.
    """
    anchors = {}
    if pages_dir:
        for path in sorted(Path(pages_dir).glob("*.html")):
            for anchor in _ids(path) or ():
                anchors.setdefault(anchor, set()).add(path.stem)
    found = {}
    for row in _MYSQL_ROW.finditer(text):
        page, anchor, name = row.group(1), row.group(2), _text(row.group(3))
        cells = [_text(cell) for cell in re.findall(r"<td>(.*?)</td>", row.group(4), re.S)]
        if len(cells) < 3 or cells[2] != "Yes" or not _NAME.fullmatch(name) or name in found:
            continue
        if not pages_dir:
            found[name] = (page, anchor)
            continue
        sysvar = "sysvar_" + name
        if page in anchors.get(sysvar, ()):
            found[name] = (page, sysvar)
        elif anchors.get(sysvar):
            pages = sorted(anchors[sysvar])
            found[name] = ("server-system-variables" if "server-system-variables" in pages else pages[0], sysvar)
        elif page in anchors.get(anchor, ()):
            found[name] = (page, anchor)
    return found


def mariadb_links(text, pages_dir=None):
    """{name: (page, anchor)} from MariaDB's full list of variables.

    A link without an anchor points at the variable's own heading, whose
    anchor is the name. With *pages_dir* an anchor the linked page does not
    define moves to the saved "...system-variables" page that defines it
    (the list sends gtid_domain_id to .../standard-replication/gtid, while
    .../gtid/gtid-system-variables documents it); otherwise it is dropped.
    """
    defined = {}
    if pages_dir:
        root = Path(pages_dir, "docs")
        for path in sorted(root.rglob("*.html")):
            page = path.relative_to(root).as_posix()[:-len(".html")]
            for anchor in _ids(path) or ():
                defined.setdefault(anchor, []).append(page)
    found = {}
    for page, anchor, label in _MARIADB_LINK.findall(text):
        name = _text(label)
        anchor = anchor or name
        if name != anchor or not _NAME.fullmatch(name) or name in found:
            continue
        if "status-variables" in page and "system" not in page:
            continue
        if pages_dir and page not in defined.get(anchor, ()):
            others = [p for p in defined.get(anchor, ()) if p.endswith("system-variables")]
            if len(others) != 1:
                continue
            page = others[0]
        found[name] = (page, anchor)
    # Some variables have a heading on a variable page but no entry in the
    # list (innodb_stats_on_metadata). A heading whose lowercase text is its
    # id, on a saved page whose name says system-variable(s), documents it.
    headed = {}
    if pages_dir:
        for path in sorted(Path(pages_dir, "docs").rglob("*.html")):
            page = path.relative_to(Path(pages_dir, "docs")).as_posix()[:-len(".html")]
            if "system-variable" not in page and "system-status-variables" not in page:
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
            for match in _HEADING.finditer(body):
                anchor, name = match.group(2), _text(match.group(3)).lower()
                if name == anchor and "_" in name and _NAME.fullmatch(name):
                    headed.setdefault(name, []).append(page)
    for name, pages in sorted(headed.items()):
        if name not in found:
            preferred = [p for p in pages if p.endswith("/server-system-variables")]
            if len(set(pages)) == 1 or preferred:
                found[name] = ((preferred or pages)[0], name)
    return found


def percona_links(directory):
    """{name: (page, anchor)} from a saved Percona manual (Percona Server or
    Percona XtraDB Cluster)."""
    found = {}
    root = Path(directory)
    for path in sorted(root.rglob("*.html")):
        page = path.relative_to(root).as_posix()
        if page.startswith("release-notes/"):
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        heads = list(_HEADING.finditer(body))
        for i, match in enumerate(heads):
            # Variable names are case-insensitive; headings write some in mixed case (wsrep_OSU_method).
            name = _text(match.group(3)).lower()
            if not _NAME.fullmatch(name) or "_" not in name:
                continue
            end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            section = body[match.end():end]
            if "<table" in section and re.search(r">\s*(Scope|Dynamic|Command[- ][Ll]ine)\s*:?\s*<", section):
                found.setdefault(name, (page, match.group(2)))
    return found


def _index(values, item):
    if item not in values:
        values.append(item)
    return values.index(item)


def _manuals(found_by_version, pages, cell):
    """{name: value}: one value when every manual agrees, else one per manual (None: absent)."""
    versions = sorted(found_by_version, key=lambda v: tuple(int(p) for p in v.split(".")))
    entries = {}
    for name in sorted({n for found in found_by_version.values() for n in found}):
        cells = tuple(cell(pages, found_by_version[v][name]) if name in found_by_version[v] else None
                      for v in versions)
        entries[name] = cells[0] if len(set(cells)) == 1 and cells[0] is not None else cells
    return versions, entries


def _block(out, label, value_lines):
    out.append("%s = {" % label)
    out.extend(value_lines)
    out.append("}")


def render(mysql, mariadb, percona, pxc, saved_on):
    """The module text. A location is a page index, or (page index, anchor)
    when the anchor is not the default for that manual. Percona keeps only
    names the MySQL manuals do not document: those link to MySQL."""
    out = ["# Generated by scripts/gen_sysvar_docs.py; do not edit.",
           "# Where the official manuals document each system variable; nothing here is fetched at run time.",
           "# Regenerate from saved reference pages (see the script's docstring); pages saved on %s." % saved_on,
           "# A location is a page index, or (page index, anchor) when the anchor differs from the default.",
           "# A tuple of locations has one per manual version (None: that manual does not document it).",
           ""]
    pages = []
    defaults = {v: {n: (page, None if anchor == "sysvar_" + n else anchor) for n, (page, anchor) in found.items()}
                for v, found in mysql.items()}
    versions, entries = _manuals(defaults, pages, lambda pages, loc: _index(pages, loc[0]) if loc[1] is None
                                 else (_index(pages, loc[0]), loc[1]))
    out.append("MYSQL_URL = 'https://dev.mysql.com/doc/refman/%s/en/%s.html#%s'  # default anchor: sysvar_<name>")
    out.append("MYSQL_VERSIONS = %r" % (tuple(versions),))
    out.append("MYSQL_PAGES = (\n%s\n)" % "\n".join("    %r," % page for page in pages))
    _block(out, "MYSQL", ["    %r: %r," % item for item in sorted(entries.items())])
    out.append("")
    pages = []
    rows = sorted((name, _index(pages, page) if anchor == name else (_index(pages, page), anchor))
                  for name, (page, anchor) in mariadb.items())
    out.append("MARIADB_URL = 'https://mariadb.com/docs/%s#%s'")
    out.append("MARIADB_PAGES = (\n%s\n)" % "\n".join("    %r," % page for page in pages))
    _block(out, "MARIADB", ["    %r: %r," % item for item in rows])
    out.append("")
    documented = {name for found in mysql.values() for name in found}
    only = {v: {n: (page, None if anchor == n else anchor) for n, (page, anchor) in found.items() if n not in documented}
            for v, found in percona.items()}
    pages = []
    versions, entries = _manuals(only, pages, lambda pages, loc: _index(pages, loc[0]) if loc[1] is None
                                 else (_index(pages, loc[0]), loc[1]))
    out.append("PERCONA_URL = 'https://docs.percona.com/percona-server/%s/%s#%s'")
    out.append("PERCONA_VERSIONS = %r" % (tuple(versions),))
    out.append("PERCONA_PAGES = (\n%s\n)" % "\n".join("    %r," % page for page in pages))
    _block(out, "PERCONA", ["    %r: %r," % item for item in sorted(entries.items())])
    out.append("")
    documented |= {name for found in percona.values() for name in found}
    only = {v: {n: (page, None if anchor == n else anchor) for n, (page, anchor) in found.items() if n not in documented}
            for v, found in pxc.items()}
    pages = []
    versions, entries = _manuals(only, pages, lambda pages, loc: _index(pages, loc[0]) if loc[1] is None
                                 else (_index(pages, loc[0]), loc[1]))
    out.append("PXC_URL = 'https://docs.percona.com/percona-xtradb-cluster/%s/%s#%s'")
    out.append("PXC_VERSIONS = %r" % (tuple(versions),))
    out.append("PXC_PAGES = (\n%s\n)" % "\n".join("    %r," % page for page in pages))
    _block(out, "PXC", ["    %r: %r," % item for item in sorted(entries.items())])
    return "\n".join(out) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mysql", action="append", default=[], metavar="VERSION=FILE")
    parser.add_argument("--mysql-pages", action="append", default=[], metavar="VERSION=DIR")
    parser.add_argument("--mariadb", metavar="FILE")
    parser.add_argument("--mariadb-pages", metavar="DIR")
    parser.add_argument("--percona", action="append", default=[], metavar="VERSION=DIR")
    parser.add_argument("--pxc", action="append", default=[], metavar="VERSION=DIR")
    parser.add_argument("--saved-on", required=True, metavar="YYYY-MM-DD", help="When the pages were saved.")
    parser.add_argument("-o", "--output", default="myflames/sysvar_docs.py")
    args = parser.parse_args(argv)
    if not args.mysql or not args.mariadb or not args.percona or not args.pxc:
        parser.error("--mysql, --mariadb, --percona and --pxc are all required.")
    page_dirs = dict(_pair(v) for v in args.mysql_pages)
    mysql = {}
    for version, path in (_pair(v) for v in args.mysql):
        mysql[version] = mysql_links(Path(path).read_text(encoding="utf-8", errors="replace"),
                                     page_dirs.get(version))
    mariadb = mariadb_links(Path(args.mariadb).read_text(encoding="utf-8", errors="replace"), args.mariadb_pages)
    percona = {version: percona_links(path) for version, path in (_pair(v) for v in args.percona)}
    pxc = {version: percona_links(path) for version, path in (_pair(v) for v in args.pxc)}
    Path(args.output).write_text(render(mysql, mariadb, percona, pxc, args.saved_on), encoding="utf-8")
    print("MySQL %s; MariaDB %d; Percona Server %s; Percona XtraDB Cluster %s" % (
        ", ".join("%s %d" % (v, len(f)) for v, f in sorted(mysql.items())), len(mariadb),
        ", ".join("%s %d" % (v, len(f)) for v, f in sorted(percona.items())),
        ", ".join("%s %d" % (v, len(f)) for v, f in sorted(pxc.items()))))


if __name__ == "__main__":
    main()
