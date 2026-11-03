# MyFlames Gemini Rules
Project rules are in AGENTS.md. Read that file whether or not this tool expands the import below.
@AGENTS.md

---

## Pre-flight gate

AGENTS.md holds the shared reliability rules (live validation, engine ground truth, reviewed SQL equals executed SQL, layout wrappers). Before declaring any task done, reporting success, or launching the desktop app, run and pass these four audits:
1. **Write safety and permissions**: no unreviewed write or review-token bypass is possible.
2. **Engine correctness**: statements, parameters and variable names match the engine source or official documentation.
3. **Live daemon smoke test**: HTTP endpoints return `200` with parsed data against live running servers.
4. **Visual and layout regression**: page scrolling, sticky positioning and navigation work across viewports.
