# Generate Demos — myflames

Regenerate `docs/demos/` with `scripts/regenerate_docs_demos.py`. It reads a
backup of the current tree and rebuilds every file-backed HTML report and SVG
with `python3 -m myflames`, using the `fixture_path` in each backup `*.json`
sidecar, into the subfolders (`mysql-basic/`, `mysql-analysis/`, ...). It also
rebuilds the four short-name analysis SVGs, `mysql-query-report.html` and
`mysql-query-compare.html`. `live-*` demos have no fixture, so they are copied
verbatim from the backup (re-capturing them needs a live server).

From the repository root:

```bash
BAK=$(mktemp -d)/demos
cp -a docs/demos "$BAK"
python3 scripts/regenerate_docs_demos.py "$BAK"
```

It exits 2 on a bad argument and 1 when a sidecar names a missing fixture.

Then check the result:

```bash
git status --short docs/demos
python3 -c "
import glob, re
svgs = glob.glob('docs/demos/**/*.svg', recursive=True)
errors = []
for f in svgs:
    svg = open(f).read()
    if '<svg' not in svg:
        errors.append(f + ': missing <svg tag')
    m_h = re.search(r'height=\"(\d+)\"', svg)
    m_vb = re.search(r'viewBox=\"0 0 [\d.]+ ([\d.]+)\"', svg)
    if m_h and m_vb and m_h.group(1) != m_vb.group(1):
        errors.append('%s: height/viewBox mismatch (%s vs %s)' % (f, m_h.group(1), m_vb.group(1)))
print(len(svgs), 'SVG files;', 'ERRORS: ' + '; '.join(errors) if errors else 'all OK')
"
```

Review the diff before committing.
