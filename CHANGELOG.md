# Changelog

All notable changes to GraphNav are documented here. Versions follow [Semantic Versioning](https://semver.org/).

---

## [1.2.3] — 2026-06-12

### Fixed
- **Flat projects with `src/`/`tests/` subfolders are no longer misdetected as monorepos.** A repo is treated as a monorepo only when a subdirectory has its own manifest (e.g. `package.json`, `pyproject.toml`); otherwise the whole repo is mapped as one graph, so root-level code is never orphaned.
- A malformed `config.toml` no longer crashes every command (falls back to defaults with a warning); a foreign `config.toml` (e.g. Hugo's) with none of graphnav's sections is ignored instead of spamming warnings; wrong-typed config values are replaced with defaults instead of raising `TypeError`.
- `graphnav neighbors`/`impact` now prefer an exact symbol-name match (`main` no longer resolves to `test_main`).
- A corrupt or mid-write `graph.json` no longer crashes `map`, the `watch` daemon, `find`/`neighbors`/`impact`, or the MCP tools — all report a friendly "run `graphnav map`" message and `watch` retries on the next update.
- `graphnav watch` now shuts down cleanly on SIGTERM (not just Ctrl-C), terminating its `graphify` child instead of orphaning it.
- `GraphNav` now reads graphs that use an `edges` key instead of `links`, matching every other reader.
- `.env` files saved with a UTF-8 BOM are parsed correctly; `OPENAI_KEY`/`GEMINI_KEY`/`DEEPSEEK_KEY` are now accepted as aliases like `ANTHROPIC_KEY`.
- All generated files are written/read as UTF-8 explicitly, fixing crashes on Windows default encodings.
- Ctrl-C during `graphnav map` exits cleanly instead of printing a traceback.
- `graphnav doctor` recognizes single-project (flat) repos instead of failing the services check.
- Truncated inline context packs no longer emit unbalanced code fences.

---

## [1.2.2] — 2026-06-12

### Added
- **One-command setup.** Running bare `graphnav` from any project root now does the entire setup — auto-detects the project shape, builds the graph, and writes all agent instruction files — then stops with a "nothing else to run" message. No need to know `map`.
- **Single-folder project support.** `graphnav` now works on a flat repo (code at the root with no service subfolders), mapping the whole repo as one graph. Previously it errored with "No services detected".

### Changed
- README and CLI help reframed around the single `graphnav` command; `context`/`serve`/`find`/`neighbors`/`impact` are now clearly labeled as agent-facing commands you rarely run by hand.

### Fixed
- `graphify` binary lookup now also searches every install scheme's scripts directory (via `sysconfig`), so it resolves under `pipx`, `--user`, and other installs where the scripts dir isn't on `PATH`.

---

## [1.2.1] — 2026-06-11

### Fixed
- `graphify` binary lookup now falls back to the Python interpreter's `bin` directory when it isn't on `PATH` (affects `pip install --user` and un-activated venv installs).

---

## [1.2.0] — 2026-06-11

### Added
- `graphnav doctor` — validates the entire setup (binary, config, graph freshness, API key, services, index cache) in one command, exits non-zero only on hard failures.
- Shared graph bundle loader with on-disk pickle cache; repeated `find`/`context`/`neighbors`/`impact` calls no longer re-parse `graph.json`. Set `GRAPHNAV_NO_CACHE=1` to opt out.
- Git-recency ranking signal — recently-changed files get a nudge up in context results. Configurable via `recency_boost_weight` in `config.toml`.
- Relation-weighted call-edge expansion — `calls`/`inherits` edges count more than `imports` above `references` when expanding graph neighbours. Configurable via `[query.edge_relation_weights]`.
- Fuzzy symbol fallback — `find`, `neighbors`, and `impact` fall back to closest-matching symbols on a miss and flag the result.
- Config validation with unknown-key warnings at startup.
- `graphnav watch` now debounces rapid file saves, skips artifact rewrites when content is unchanged (no mtime churn), and restarts the underlying `graphify watch` process with exponential backoff (1 s → 60 s cap) on crash.
- MCP server reloads the graph automatically when `graph.json` changes; CLI queries are cached per process.

---

## [1.1.0] — 2026-05-01

### Added
- `graphnav serve` — MCP server over stdio exposing `graph_context`, `graph_find`, `graph_neighbors`, `read_region`, and `impact` as native agent tools. `mcp` promoted to a core dependency so this works on a plain `pip install graphnav`.
- `graphnav impact` — blast-radius query: lists every symbol that would break if you change the target.
- Git-SHA staleness detection — `graphnav context` prefixes output with a warning when the graph was built on an older commit than the current `HEAD`.
- Call-edge expansion in ranking — files reachable from strong query matches are pulled in via graph edges even when their own text doesn't match.
- `graphnav context` now defaults to inline code regions (pass `--locations-only` for the old `file:line` index).

### Changed
- Package renamed to `graphnav` (was `codex-graphify` / `repomap`).
- Agent instruction blocks are now written behind `<!-- graphnav:start -->` / `<!-- graphnav:end -->` markers so re-running `map` doesn't clobber hand-written content.
- `CLAUDE.md` written alongside `AGENTS.md` and `.github/copilot-instructions.md`.

---

## [0.1.0] — 2026-04-15

Initial release.

- `graphnav map` — auto-detects service boundaries, extracts a single overarching Graphify knowledge graph, partitions it per service, writes `SYMBOLS.md`, `BRIDGES.md`, `MONOREPO_MAP.md`, and coding playbooks to `CLAUDE.md`, `AGENTS.md`, and `.github/copilot-instructions.md`.
- `graphnav context` — token-budgeted context pack for a coding task, free and instant (no LLM call).
- `graphnav watch` — long-running daemon keeping all graphs and generated files up to date.
- `graphnav find` / `neighbors` — shell graph queries.
- Single overarching graph across the whole monorepo surfaces cross-service call edges that per-service extraction misses.
- `config.toml` support for backend, poll interval, token budget, skip dirs, and ranking weights.
