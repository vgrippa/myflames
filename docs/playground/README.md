# myflames browser playground

A zero-install, client-side playground: paste `EXPLAIN ANALYZE FORMAT=JSON`, get a flame graph, diagram, digest, or token-savings report — all rendered in the browser. myflames is pure-Python stdlib, so it runs entirely under [Pyodide](https://pyodide.org); nothing is uploaded anywhere.

## Run it locally

```bash
python3 -m http.server -d docs/playground 8000
# open http://localhost:8000
```

On load the page boots Pyodide and installs myflames via `micropip`.

## Two install sources

`index.html` installs myflames one of two ways (see the `WHEEL_URL` constant near the top of the inline script):

- **From PyPI (default):** `micropip.install("myflames")` — works once the package is published to PyPI. (Not yet published as of this writing.)
- **From a local wheel:** build a wheel and point `WHEEL_URL` at it, served from this directory:
  ```bash
  python3 -m build            # produces dist/myflames-*.whl
  cp dist/myflames-*.whl docs/playground/
  # then set WHEEL_URL = "./myflames-1.5.0-py3-none-any.whl" in index.html
  ```

## Status

This is a working scaffold. The Python call paths it uses (`myflames.render.render_explain`, `myflames.tokens`) are the real public API and are validated by the test suite. The one external dependency is a reachable myflames wheel — PyPI publish or a locally-served build. Until then the page loads and reports the install source it could not reach, rather than failing silently.
