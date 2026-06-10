# graphnav

**Token-cheap AI coding for monorepos.** Builds a graphify knowledge graph of your codebase, then gives every AI coding agent (GitHub Copilot, Claude Code, OpenAI Codex) a minimal, targeted context pack instead of the whole repo.

---

## The problem

AI coding agents default to exploring the filesystem with `find`/`ls`/`cat`, reading entire files, and burning tokens on irrelevant code. In a monorepo this compounds: every request pulls context from every service.

graphnav solves this by:

1. Extracting a knowledge graph (symbols, call edges, cross-service links) once, up front
2. Giving agents a **one-command retrieval path** that returns only the files and `file:line` locations relevant to the current task
3. Writing instruction files that explicitly direct agents to use retrieval first — and ban raw filesystem exploration

---

## Install

```bash
pip install graphnav
```

Requires Python ≥ 3.11. Pulls `graphifyy` (the `graphify` binary) automatically.

**API key:** Place a `.env` file anywhere up the directory tree from your project (or inside any service subfolder). graphnav walks up and down to find it:

```
ANTHROPIC_KEY=sk-ant-...
```

---

## Quickstart

```bash
# In your monorepo root — detects services, builds graphs, writes agent instructions
graphnav map

# Get a context pack for a task (free, no LLM, ~instant)
graphnav context "add a critique scoring function to the coach"

# Keep graphs live as you edit
graphnav watch
```

After `map`, every AI agent in the repo has access to:

- **`CLAUDE.md`** — picked up by Claude Code
- **`AGENTS.md`** — picked up by OpenAI Codex CLI
- **`.github/copilot-instructions.md`** — picked up by GitHub Copilot
- **`<service>/graphify-out/SYMBOLS.md`** — symbol→`file:line` index per service
- **`<service>/graphify-out/BRIDGES.md`** — exact cross-service call sites with line numbers
- **`graphify-out/MONOREPO_MAP.md`** — overview of all services and their connections

---

## Commands

### `graphnav map`

Builds the knowledge graph and generates all agent instruction files.

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

Press `Ctrl-C` to stop cleanly.

---

### `graphnav serve` (MCP server)

Runs an [MCP](https://modelcontextprotocol.io) server over stdio so AI agents call the graph tools **natively** — no need to remember to run `context` by hand. The graph is loaded once and reused across calls.

```
pip install 'graphnav[mcp]'
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

---

### `graphnav` (no subcommand)

If run with no arguments in a monorepo root, auto-detects services and runs `map` automatically. If a prompt is given, falls through to the context-injection path for the Codex CLI.

---

## Service detection

graphnav detects a subdirectory as a service if it contains:

- A marker file: `package.json`, `pyproject.toml`, `requirements.txt`, `go.mod`, `Cargo.toml`, `tsconfig.json`, `Gemfile`, and more, **or**
- Any source code files (`.py`, `.ts`, `.tsx`, `.js`, `.go`, `.rs`, `.java`, etc.)

Skipped automatically: `node_modules`, `dist`, `build`, `graphify-out`, `__pycache__`, `.git`, dotdirs, and other non-source directories.

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

---

## Configuration

Place a `config.toml` in the project root (or pass `--config PATH`):

```toml
[mono]
graphify_backend = "claude"        # LLM backend for extraction
watch_poll_interval = 3.0          # seconds between mtime checks in watch mode
context_budget_tokens = 2000       # token budget for graphnav context output
context_top_files = 8              # max files returned by context command

[query]
edge_boost_weight = 0.4            # boost files connected to high-ranking files via call edges (0 disables)

[graph]
skip_patterns = ["node_modules", ".git", "graphify-out", "playwright-report"]
```

Ranking is BM25 over graph symbols, plus a community boost, plus **call-edge expansion**: a file connected to a strong match gets pulled in even when its own text doesn't match the query (e.g. the endpoint you must edit for a "rate limit" task, reached from the matching `rate_limiter` symbol). Set `edge_boost_weight = 0` to disable.

---

## How cross-service bridges work

graphnav extracts **one overarching graph** of the whole repo (not one per service). This means graphify's AST and semantic extraction can find call edges that cross service boundaries — something a per-service extraction followed by a union merge can never do.

The overarching graph is then partitioned into per-service local graphs for navigation. Bridges are derived from the overarching graph where an edge's endpoints belong to different services.

**Note:** Bridges only appear for direct code references (imports, function calls). Services that communicate over HTTP (e.g. a React frontend calling a Python backend via `fetch`) will correctly show zero bridges — the connection exists at the protocol level, not the code level.

---

## Team setup

Every team member runs one command after cloning:

```bash
pip install graphnav
```

Drop a `.env` with your API key anywhere in or above the repo:

```
ANTHROPIC_KEY=sk-ant-...
```

Then:

```bash
graphnav map          # one-time setup, or re-run after large refactors
graphnav watch        # optional: keep graphs live during active development
```

The generated `CLAUDE.md`, `AGENTS.md`, and `.github/copilot-instructions.md` can be committed to the repo so teammates get the agent instructions without needing to re-run `map`.

---

## Requirements

- Python ≥ 3.11
- `graphifyy` ≥ 0.8 (installed automatically)
- An API key for your chosen LLM backend (only needed for `map` / `watch`; `context` is free)
