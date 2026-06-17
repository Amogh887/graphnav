# GraphNav: Leveraging Graphify for Token-Cheap AI Coding in Monorepos

**GraphNav** gives every AI coding agent (GitHub Copilot, Claude Code, OpenAI Codex) a minimal, targeted context pack instead of the whole repo. It is built on **[Graphify](https://pypi.org/project/graphifyy/)**: GraphNav extracts a Graphify knowledge graph of your codebase once, then serves each agent only the files and `file:line` locations relevant to the current task — so agents stop burning tokens on `find`/`ls`/`cat` exploration. What makes GraphNav unique is that it extracts a **single overarching Graphify graph** across the entire monorepo (not one graph per service), letting it surface call edges that cross service boundaries — something per-service extraction can never do.

> **Works with no API key — no network, no cost, no data leaves your machine.** By default `graphnav` builds the graph locally from your code's AST. An LLM is used only if you explicitly opt in (see [Is this safe?](#is-this-safe)).

---

## Is this safe?

Short answer: **yes — the default path is fully local, free, and non-destructive.**

- **No network, no LLM, no cost by default.** `graphnav` and `graphnav map` build the knowledge graph on your machine from your code's AST (via the local `graphify` binary). Nothing is sent anywhere. This is the default whenever no API key is configured.
- **No telemetry.** GraphNav makes no analytics or "phone-home" calls, ever.
- **The only outward-facing actions are opt-in and announced.** (1) `graphnav map --semantic` (or setting `[mono] semantic = true`) uses an LLM backend for richer links — this sends your source to the provider you choose and may incur cost, and the tool prints a notice before it does. (2) `graphnav "<prompt>"` invokes the external `codex` CLI. Neither runs unless you ask for it.
- **`.env` handling.** GraphNav reads `.env` files **only** to pass an LLM API key into the local `graphify` subprocess for the opt-in semantic build. Keys are never logged and nothing is sent anywhere except the LLM backend you explicitly choose. With no key, `.env` is irrelevant.
- **What it writes:** a `graphify-out/` graph directory per project, and agent instruction files (`CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`). Nothing outside your repo.
- **Provenance.** MIT-licensed; source at [github.com/Amogh887/leveraging-graphify](https://github.com/Amogh887/leveraging-graphify); published to PyPI via GitHub Actions Trusted Publishing (OIDC) with signed provenance attestations.

> **Note on names:** the PyPI package is **`graphnav`** and it imports as `graphnav`. Its dependency is published on PyPI as **`graphifyy`** (double-y, because `graphify` was already taken) but installs the `graphify` binary and imports as `graphify`.

---

## Why GraphNav

AI coding agents default to exploring the filesystem with `find`/`ls`/`cat`, reading entire files, and burning tokens on irrelevant code. In a monorepo this compounds: every request pulls context from every service.

GraphNav solves this by:

1. Extracting a knowledge graph (symbols, call edges, cross-service links) once, up front
2. Giving agents a **one-command retrieval path** that returns only the files and `file:line` locations relevant to the current task
3. Writing instruction files that explicitly direct agents to use retrieval first — and ban raw filesystem exploration

---

## GraphNav Core Features

- **Always fresh, automatically** — when source files change, the next graph query detects it and rebuilds the graph in the background. No daemon, no manual re-runs, nothing to remember.
- **Token-budgeted context packs** — `graphnav context "<task>"` returns only the relevant code, inline, with no LLM call.
- **Native MCP tools** — `graphnav serve` exposes the graph to agents over the Model Context Protocol, refreshed automatically when the graph changes.
- **Graph-aware ranking** — BM25 plus relation-weighted call-edge expansion and a git-recency nudge, so the file you actually need to edit surfaces even when its text doesn't match the query.
- **Fuzzy symbol search** — `find`/`neighbors`/`impact` fall back to closest-match symbols when an exact lookup misses a typo.
- **Cross-service bridges** — a single overarching Graphify graph exposes call edges that cross service boundaries.
- **Self-diagnosing** — `graphnav doctor` validates the whole setup in one command.

---

## Setup is one command

```bash
pip install graphnav
graphnav
```

That's the whole setup. Run `graphnav` from your project root and it:

1. Auto-detects your project — a **single folder** or a **monorepo** of services
2. Builds the knowledge graph
3. Writes the agent instruction files

Then open the repo in your AI coding tool and start working. **There is nothing else to run — ever.** When you edit files, the next graph query (from you or your agent) automatically rebuilds the graph in the background, so it never goes stale. Disable with `auto_rebuild = false` under `[mono]` or `GRAPHNAV_NO_AUTO_REBUILD=1`.

Requires Python ≥ 3.11. Pulls `graphifyy` (the `graphify` binary) automatically — including under `pipx`, `--user`, and virtualenv installs.

**API key (optional, opt-in):** By default `graphnav` builds a free AST-only graph — symbols, call edges, and cross-service bridges all work out of the box, fully local, even if a key is present. To get richer semantic links you explicitly opt in with `graphnav map --semantic` (or `[mono] semantic = true`), which sends your source to the LLM backend you choose. Provide the key via a `.env` file anywhere up the directory tree from your project (or inside any service subfolder), and graphnav walks up and down to find it. Any backend's key works — Anthropic, OpenAI, Gemini, or DeepSeek:

```
OPENAI_API_KEY=sk-...
ANTHROPIC_KEY=sk-ant-...
```

After running `graphnav`, every AI agent in the repo has access to:

- **`CLAUDE.md`** — picked up by Claude Code
- **`AGENTS.md`** — picked up by OpenAI Codex CLI
- **`.github/copilot-instructions.md`** — picked up by GitHub Copilot
- **`<service>/graphify-out/SYMBOLS.md`** — symbol→`file:line` index per service
- **`<service>/graphify-out/BRIDGES.md`** — exact cross-service call sites with line numbers
- **`graphify-out/MONOREPO_MAP.md`** — overview of all services and their connections

### Optional

```bash
graphnav watch     # eager mode: rebuild on every save instead of at query time
graphnav doctor    # diagnose the setup if something looks wrong
```

---

## Commands

> **You only need `graphnav`.** Running it bare does the full setup (it runs `map` for you). `watch` and `doctor` are the only other commands you'd type by hand. Everything below `graphnav watch` — `context`, `serve`, `find`, `neighbors`, `impact` — is meant for your **AI agent** to call automatically (via the generated instruction files or the MCP server), not for you to run manually. They're documented here for completeness.

### `graphnav` (just this)

From your project root, run `graphnav` with no arguments. It auto-detects whether you have a single-folder project or a monorepo, builds the knowledge graph, writes all agent instruction files, and stops. This is the one command you run.

```
graphnav
```

For a single-folder project it maps the whole repo as one graph; for a monorepo it builds per-service graphs and cross-service bridges. Equivalent to running `graphnav map` explicitly.

---

### `graphnav map`

The build step that bare `graphnav` runs for you. Builds the knowledge graph and generates all agent instruction files.

```
graphnav map [--root PATH] [--backend BACKEND] [--dry-run]
```

| Flag | Default | Description |
|---|---|---|
| `--root` | `.` | Monorepo root directory |
| `--backend` | `claude` | LLM backend for extraction: `claude`, `openai`, `gemini`, `deepseek`, `ollama` |
| `--dry-run` | off | Detect services and print the plan without calling graphify |

What it does:
1. Auto-detects service boundaries (by marker files **and** by source code presence)
2. Extracts a single overarching knowledge graph of the whole repo via graphify
3. Partitions it into per-service local graphs
4. Analyzes cross-service edges and writes `BRIDGES.md` per service
5. Writes `SYMBOLS.md`, `MONOREPO_MAP.md`, and the coding playbook to `CLAUDE.md`, `AGENTS.md`, `.github/copilot-instructions.md`

---

### `graphnav context`

Prints a token-budgeted context pack for a coding task. **No LLM call — free and instant.** Defaults to inline code regions; pass `--locations-only` for a `file:line` index instead.

```
graphnav context "<task>" [--root PATH] [--budget N] [--files N] [--locations-only]
```

If the graph was built on an older commit than the current `HEAD`, the pack is prefixed with a staleness warning (it tracks the build-time git SHA in `graphify-out/.graphnav-meta.json`) so drifted line numbers don't silently mislead the agent.

| Flag | Default | Description |
|---|---|---|
| `--budget` | `2000` | Approximate token budget for the output |
| `--files` | `8` | Max number of files to include |
| `--root` | `.` | Repo root |

Example output:

```
# Context for: add a critique scoring function to the coach

## Open only these files
- backend/coach.py — generate_response() L145, practice_critique() L326
- eval/run_eval.py — run_prompts_on_dataset() L78, judge_responses() L119

## Cross-service impact
- eval/run_eval.py:run_prompts_on_dataset() L78 --calls--> backend/coach.py:generate_response() L145

## Next
Read only the file:line regions above. Before changing a symbol under
Cross-service impact, run `graphify affected "<symbol>"`. Then run the tests.
```

Works on single-service repos too (the Cross-service section is omitted).

---

### `graphnav watch`

Long-running daemon. Watches the repo for file changes and keeps all graphs, symbol maps, bridge notes, and agent instructions up to date.

```
graphnav watch [--root PATH] [--backend BACKEND]
```

It only rewrites generated artifacts (`SYMBOLS.md`, `BRIDGES.md`, per-service graphs, agent instructions) when their content actually changes, so it won't churn file mtimes and retrigger your editor or agents. A change is acted on after one quiet poll (debounce), and if the underlying `graphify watch` process dies it is restarted with exponential backoff (1s → 2s → … capped at 60s). Press `Ctrl-C` to stop cleanly.

---

### `graphnav serve` (MCP server)

Runs an [MCP](https://modelcontextprotocol.io) server over stdio so AI agents call the graph tools **natively** — no need to remember to run `context` by hand. The graph is loaded once and cached, then automatically reloaded whenever `graph.json` changes (e.g. after a `map` or while `watch` runs), so a long-lived server never serves stale line numbers. The MCP runtime ships with GraphNav, so this works on a plain `pip install graphnav`.

```
graphnav serve [--root PATH]
```

Exposes five tools:

| Tool | Use |
|---|---|
| `graph_context(task)` | Minimal context pack with relevant code inline — the first-resort tool |
| `graph_find(query)` | Find symbols by query → `file:line` |
| `graph_neighbors(symbol)` | A symbol's callers and callees |
| `read_region(path, start, end)` | Read a line range instead of a whole file |
| `impact(symbol)` | Blast radius: who breaks if you change this symbol |

Register it with any MCP client. For Claude Code:

```json
{
  "mcpServers": {
    "graphnav": { "command": "graphnav", "args": ["serve", "--root", "."] }
  }
}
```

---

### `graphnav find` / `neighbors` / `impact`

Quick graph queries from the shell (no LLM):

```
graphnav find "rate limit"          # symbols matching a query → file:line
graphnav neighbors create_incident  # callers + callees
graphnav impact rate_limiter        # blast radius before changing a symbol
```

If an exact lookup finds nothing (e.g. a typo like `create_incdnet`), GraphNav falls back to the closest-matching symbols and flags the result so you know it's a guess.

---

### `graphnav doctor`

Diagnoses a GraphNav setup in one command — run it when something isn't working. No LLM call.

```
graphnav doctor [--root PATH] [--config PATH]
```

It checks the `graphify` binary, the config file (and reports any validation warnings), the graph's existence/validity and staleness, whether an API key is discoverable for your backend, the detected services, and the index cache. It exits non-zero only if a check **fails** (warnings don't):

```
  [ok] graphify binary — /usr/local/bin/graphify (graphify 0.8.2)
  [ok] config — /repo/config.toml
  [ok] graph.json — 1843 nodes, 5120 links
  [warn] graph meta — graph is behind HEAD — re-run `graphnav map`
  [ok] API key — found in environment ($ANTHROPIC_API_KEY)
  [ok] services — 3 detected: api, backend, web
  [ok] index cache — warm

6 ok, 1 warn, 0 fail
```

---

### `graphnav` (no subcommand)

Run with no arguments from any project root, it auto-detects the project shape (single folder or monorepo) and runs the full setup automatically. If a prompt is given, it falls through to the graph-context path for the Codex CLI (this path invokes the external `codex` CLI — see [Is this safe?](#is-this-safe)).

---

## Service detection

graphnav detects a subdirectory as a service if it contains:

- A marker file: `package.json`, `pyproject.toml`, `requirements.txt`, `go.mod`, `Cargo.toml`, `tsconfig.json`, `Gemfile`, and more, **or**
- Any source code files (`.py`, `.ts`, `.tsx`, `.js`, `.go`, `.rs`, `.java`, etc.)

Skipped automatically: `node_modules`, `dist`, `build`, `graphify-out`, `__pycache__`, `.git`, dotdirs, and other non-source directories. Add your own names via `extra_skip_dirs` in `[mono]`.

---

## Generated files

### `CLAUDE.md` / `AGENTS.md` / `.github/copilot-instructions.md`

All three contain the same managed block — the coding playbook. Content is written between `<!-- graphnav:start -->` / `<!-- graphnav:end -->` markers so re-running `map` updates only the block and preserves any hand-written content outside it.

The playbook instructs agents to:

1. Read `MONOREPO_MAP.md` first for any non-trivial task
2. Run `graphnav context "<task>"` instead of exploring with `find`/`ls`/`cat`
3. Open only the returned `file:line` regions
4. Check `graphify affected` before changing cross-service symbols
5. Skip all of the above for single-line edits

### `<service>/graphify-out/SYMBOLS.md`

Compact symbol index for the service. Example:

```
# Symbols: backend

## coach.py
- generate_response() — L145
- practice_critique() — L326
- _parse_json_response() — L79
```

Much smaller than the raw `graph.json` (tens of bytes per symbol vs. kilobytes per node).

### `<service>/graphify-out/BRIDGES.md`

Cross-service call sites with exact line numbers on both sides.

```
| Local File | Symbol | Loc | Relation | → Service | Remote File | Remote Symbol | Loc |
|---|---|---|---|---|---|---|---|
| run_eval.py | run_prompts_on_dataset() | L78 | calls | backend | backend/coach.py | generate_response() | L145 |
```

Includes a note to run `graphify affected "<symbol>"` before editing any listed symbol.

### `graphify-out/MONOREPO_MAP.md`

Overview of all services and which services each connects to.

```
| Service | Graph | Bridges To |
|---|---|---|
| api | api/graphify-out/graph.json | _none_ |
| backend | backend/graphify-out/graph.json | api |
| eval | eval/graphify-out/graph.json | backend |
```

### `graphify-out/.graphnav-cache.pkl`

An auto-managed cache of the parsed graph index so repeated `find`/`context`/`neighbors`/`impact` calls don't re-parse `graph.json` every time. It is rebuilt automatically whenever `graph.json` changes and is safe to delete at any time. Keep `graphify-out/` gitignored. The cache lives inside your own repo's output directory; loading it is no more trusting than running the rest of the tool, since anyone who could plant a malicious cache there could already rewrite `graph.json` or `CLAUDE.md`. Set `GRAPHNAV_NO_CACHE=1` to disable it.

---

## Configuration

Place a `config.toml` in the project root (or pass `--config PATH`):

```toml
[mono]
graphify_backend = "claude"        # LLM backend for extraction
auto_rebuild = true                # rebuild the graph in the background when queries find it stale
watch_poll_interval = 3.0          # seconds between mtime checks in watch mode
context_budget_tokens = 2000       # token budget for graphnav context output
context_top_files = 8              # max files returned by context command
extra_skip_dirs = []               # extra directory names to skip during service detection

[query]
edge_boost_weight = 0.4            # boost files connected to high-ranking files via graph edges (0 disables)
recency_boost_weight = 0.2         # nudge files touched in recent git commits higher (0 disables)

# Per-relation weights for edge expansion (a "calls" edge counts more than "references")
[query.edge_relation_weights]
calls = 1.0
inherits = 1.0
imports = 0.6
references = 0.3

[graph]
skip_patterns = ["node_modules", ".git", "graphify-out", "playwright-report"]
```

How Graphify enhances GraphNav ranking: it is BM25 over graph symbols, plus a community boost, plus **relation-weighted call-edge expansion** — a file connected to a strong match gets pulled in even when its own text doesn't match the query (e.g. the endpoint you must edit for a "rate limit" task, reached from the matching `rate_limiter` symbol), with `calls`/`inherits` edges weighted above `imports` above `references`. A **git-recency** signal then nudges recently-changed files up. Set `edge_boost_weight = 0` or `recency_boost_weight = 0` to disable either.

---

## How Graphify Enhances GraphNav: Cross-Service Bridges

GraphNav extracts **one overarching Graphify graph** of the whole repo (not one per service). This means Graphify's AST and semantic extraction can find call edges that cross service boundaries — something a per-service extraction followed by a union merge can never do.

The overarching graph is then partitioned into per-service local graphs for navigation. Bridges are derived from the overarching graph where an edge's endpoints belong to different services.

**Note:** Bridges only appear for direct code references (imports, function calls). Services that communicate over HTTP (e.g. a React frontend calling a Python backend via `fetch`) will correctly show zero bridges — the connection exists at the protocol level, not the code level.

---

## Team setup

Every team member runs one command after cloning:

```bash
pip install graphnav
```

Optionally drop a `.env` with any backend's API key anywhere in or above the repo for richer semantic links (without one, a free AST-only graph is built):

```
ANTHROPIC_KEY=sk-ant-...
```

Then:

```bash
graphnav              # one-time setup, or re-run after large refactors
graphnav watch        # optional: keep graphs live during active development
```

The generated `CLAUDE.md`, `AGENTS.md`, and `.github/copilot-instructions.md` can be committed to the repo so teammates get the agent instructions without needing to re-run `map`.

---

## Requirements

- Python ≥ 3.11
- `graphifyy` ≥ 0.8 (installed automatically)
- No API key required — `map`/`watch` build a free AST-only graph by default. A key for your chosen LLM backend (Anthropic, OpenAI, Gemini, or DeepSeek) is optional and only enriches semantic links; `context` and all queries are always free.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md) for a full version history.

---

## License

[MIT](LICENSE) © 2026 Amogh Rao
