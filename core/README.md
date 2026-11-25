# Workload Galaxy core

What every module shares. Python today (`py/galaxy_core`); the shared front end is still to come.

| Module | What it gives a module |
|---|---|
| `settings` | Declare settings once as `Field`s; load them from the environment or a saved dict; serve them as a schema for the module page. Secrets are write-only |
| `nodes` | Builders for nodes, edges and findings in the graph format the front end draws, and the scoring that turns findings into each node's colour |
| `graphops` | Scope a graph to some groups; summarise findings into totals and fleet patterns |
| `scale` | Semantic zoom for large estates, and the server-side tables, detail and search, with hooks for a module's own columns |
| `state` | The background refresh loop: a module implements `build()` and gets caching and last-good-graph behaviour |
| `webapp` | The HTTP API and UI serving every module exposes; a module adds its own routes |

A module supplies where its data comes from, how that becomes a graph, and what it checks for.
[`modules/esxi`](../modules/esxi) is the first one built this way and the example to copy:
`config.py` (settings), `graph.py` (builder), `findings.py` (checks), `state.py`, `main.py`.

A module's image copies `core/py/galaxy_core` in beside its own package, so it is built with
the repository root as the Docker build context (see `modules/esxi/Dockerfile`).

Tests: `python -m pytest -q core/py/tests` from the repository root, in any environment with
`pytest` installed (the core's own tests need nothing else).
