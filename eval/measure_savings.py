#!/usr/bin/env python3
"""
Agentic token savings harness for codex-graph.

Runs a task suite against a target repo in two conditions:
  baseline  — Claude + file tools, no context pack
  treatment — Claude + file tools + codex-graph context pack prepended

Accumulates response.usage across all agentic turns per session and prints a
comparison table (medians across reps) showing token consumption, tool calls,
and turns to completion.

Tasks file uses `# CATEGORY: coding|explain|irrelevant` marker lines to tag the
tasks that follow; coding tasks get a "produce a unified diff" system prompt.

A hard --budget-usd kill-switch stops the run before estimated spend exceeds the
cap (uses response.usage + per-model pricing). --out checkpoints after each task.

Usage:
    python eval/measure_savings.py --root /path/to/repo --tasks eval/tasks.txt \
        --reps 1 --model claude-haiku-4-5-20251001 --budget-usd 6 --out results.json
"""

import argparse
import json
import random
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import anthropic


TOOLS = [
    {
        "name": "read_file",
        "description": "Read the contents of a file in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path relative to the repository root.",
                }
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_directory",
        "description": "List files and subdirectories at a path in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path relative to repo root. Defaults to '.'.",
                }
            },
            "required": [],
        },
    },
    {
        "name": "search_code",
        "description": "Search for a regex pattern across source files in the repository.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Grep-compatible regex pattern to search for.",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search within, relative to repo root. Defaults to '.'.",
                },
            },
            "required": ["pattern"],
        },
    },
]

GRAPH_TOOLS = [
    {
        "name": "graph_find",
        "description": "Find the most relevant symbols (functions/classes) for a query using the codex-graph knowledge graph. Returns symbol name + file:line. Use this instead of broad text searches.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "What you are looking for, in natural language or symbol names."}},
            "required": ["query"],
        },
    },
    {
        "name": "graph_neighbors",
        "description": "Show what calls/references a symbol and what it calls (its callers and callees) from the knowledge graph. Use to find the blast radius of a change.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Symbol name (function/class) to inspect."}},
            "required": ["symbol"],
        },
    },
    {
        "name": "read_region",
        "description": "Read a specific line range of a file (cheaper than reading the whole file).",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path relative to repo root."},
                "start_line": {"type": "integer"},
                "end_line": {"type": "integer"},
            },
            "required": ["path", "start_line", "end_line"],
        },
    },
]

SEARCH_INCLUDES = [
    "*.py", "*.ts", "*.js", "*.vue", "*.go", "*.java", "*.rb", "*.cs", "*.proto",
]

MAX_FILE_CHARS = 8_000
MAX_SEARCH_CHARS = 4_000
MAX_REGION_LINES = 200
MAX_TURNS = 15

PRICING = {
    "claude-haiku-4-5-20251001": {"in": 1.00, "out": 5.00},
    "claude-sonnet-4-6": {"in": 3.00, "out": 15.00},
    "claude-opus-4-8": {"in": 15.00, "out": 75.00},
}
DEFAULT_PRICING = {"in": 15.00, "out": 75.00}

QA_SYSTEM = (
    "You are a helpful coding assistant. Answer questions about the repository "
    "by exploring it with the available tools. Be thorough but concise."
)

CODING_SYSTEM = (
    "You are a senior software engineer working in this repository. Implement the "
    "change the user requests. Explore with the available tools to locate the right "
    "code and understand existing patterns, then output the COMPLETE change as a "
    "unified diff (with `diff --git` headers and `@@` hunks) against the real files. "
    "Do not ask clarifying questions — state any assumptions briefly. You cannot "
    "write to disk; deliver the diff in your final message."
)

CONTEXT_SUFFIX = (
    "\n\nThe following context pack was generated by codex-graph and identifies the "
    "most relevant files and cross-service connections for this task. Use it to go "
    "straight to the relevant code with minimal exploration.\n\n{context_pack}"
)


@dataclass
class TurnStats:
    input_tokens: int
    output_tokens: int
    tool_calls: int


@dataclass
class SessionResult:
    condition: str
    task: str
    turns: list[TurnStats] = field(default_factory=list)
    final_answer: str = ""
    hit_turn_limit: bool = False

    @property
    def total_input(self) -> int:
        return sum(t.input_tokens for t in self.turns)

    @property
    def total_output(self) -> int:
        return sum(t.output_tokens for t in self.turns)

    @property
    def total_tool_calls(self) -> int:
        return sum(t.tool_calls for t in self.turns)

    @property
    def total_turns(self) -> int:
        return len(self.turns)


def session_cost(session: SessionResult, model: str) -> float:
    p = PRICING.get(model, DEFAULT_PRICING)
    return session.total_input / 1e6 * p["in"] + session.total_output / 1e6 * p["out"]


def _retry_after_seconds(err: Exception) -> float | None:
    try:
        ra = err.response.headers.get("retry-after")
        return float(ra) if ra else None
    except Exception:
        return None


def create_with_retry(client: anthropic.Anthropic, max_attempts: int = 12, **kwargs):
    for attempt in range(max_attempts):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            wait = _retry_after_seconds(e) or 30.0
            print(f"    [rate-limit] waiting {wait:.0f}s (attempt {attempt + 1})", file=sys.stderr)
            time.sleep(wait + random.uniform(0, 2))
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            code = getattr(e, "status_code", None)
            if code in (429, 500, 503, 529) or isinstance(e, anthropic.APIConnectionError):
                print(f"    [api {code}] backing off 20s (attempt {attempt + 1})", file=sys.stderr)
                time.sleep(20.0)
            else:
                raise
    return client.messages.create(**kwargs)


def _safe_path(repo_root: Path, relative: str) -> Path:
    resolved = (repo_root / relative).resolve()
    resolved.relative_to(repo_root.resolve())
    return resolved


def execute_tool(name: str, inputs: dict, repo_root: Path, nav=None) -> str:
    try:
        if name == "read_file":
            path = _safe_path(repo_root, inputs.get("path", ""))
            return path.read_text(errors="replace")[:MAX_FILE_CHARS]
        elif name == "list_directory":
            path = _safe_path(repo_root, inputs.get("path", "."))
            entries = sorted(path.iterdir(), key=lambda e: (e.is_file(), e.name))
            return "\n".join(e.name + ("/" if e.is_dir() else "") for e in entries)
        elif name == "search_code":
            pattern = inputs.get("pattern", "")
            search_path = str(repo_root / inputs.get("path", "."))
            includes = [f"--include={pat}" for pat in SEARCH_INCLUDES]
            result = subprocess.run(
                ["grep", "-rn", *includes, pattern, search_path],
                capture_output=True,
                text=True,
            )
            output = result.stdout[:MAX_SEARCH_CHARS]
            return output if output.strip() else "(no matches)"
        elif name == "read_region":
            path = _safe_path(repo_root, inputs.get("path", ""))
            start = max(1, int(inputs.get("start_line", 1)))
            end = min(start + MAX_REGION_LINES, int(inputs.get("end_line", start)))
            lines = path.read_text(errors="replace").splitlines()
            chunk = lines[start - 1:end]
            return "\n".join(f"{start + i:>5}  {ln}" for i, ln in enumerate(chunk)) or "(empty range)"
        elif name == "graph_find":
            if nav is None:
                return "graph unavailable"
            hits = nav.find_symbols(inputs.get("query", ""), k=8)
            return "\n".join(f"{h['symbol']} — {h['file']}:{h['loc']}" for h in hits) or "(no matches)"
        elif name == "graph_neighbors":
            if nav is None:
                return "graph unavailable"
            r = nav.neighbors(inputs.get("symbol", ""))
            if not r.get("found", True):
                return "(symbol not found)"
            parts = [f"{r['symbol']} defined at {r['defined_at']}"]
            if r.get("callers"):
                parts.append("callers:\n" + "\n".join("  " + c for c in r["callers"]))
            if r.get("callees"):
                parts.append("calls:\n" + "\n".join("  " + c for c in r["callees"]))
            return "\n".join(parts)
        else:
            return f"unknown tool: {name}"
    except (ValueError, OSError) as exc:
        return f"error: {exc}"


CODEX_GRAPH_ROOT = Path(__file__).resolve().parent.parent
if str(CODEX_GRAPH_ROOT) not in sys.path:
    sys.path.insert(0, str(CODEX_GRAPH_ROOT))

CONTEXT_SKIP_PATTERNS = [
    "node_modules", ".git", "graphify-out", "dist", "build",
    "playwright-report", "test-results", ".next", "coverage",
]


def get_context_pack(task: str, repo_root: Path) -> str:
    from codex_graph.multirepo import build_context_pack
    try:
        pack = build_context_pack(
            root=str(repo_root),
            task=task,
            top_files=8,
            budget_tokens=2000,
            skip_patterns=CONTEXT_SKIP_PATTERNS,
        )
        return pack.strip()
    except Exception as exc:
        print(f"  context pack error: {exc!r}", file=sys.stderr)
        return ""


def get_context_pack_inline(task: str, repo_root: Path) -> str:
    from codex_graph.multirepo import build_context_pack_inline
    try:
        pack = build_context_pack_inline(
            root=str(repo_root),
            task=task,
            top_files=3,
            budget_tokens=2500,
            skip_patterns=CONTEXT_SKIP_PATTERNS,
        )
        return pack.strip()
    except Exception as exc:
        print(f"  context pack error: {exc!r}", file=sys.stderr)
        return ""


def run_session(
    task: str,
    condition: str,
    system: str,
    repo_root: Path,
    model: str,
    max_turns: int = MAX_TURNS,
    tools: list | None = None,
    nav=None,
    cache_system: bool = False,
) -> SessionResult:
    client = anthropic.Anthropic(max_retries=0)
    tools = tools if tools is not None else TOOLS
    messages = [{"role": "user", "content": task}]
    session = SessionResult(condition=condition, task=task)

    system_arg = system
    if cache_system and system:
        system_arg = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    for _ in range(max_turns):
        response = create_with_retry(
            client,
            model=model,
            max_tokens=4096,
            system=system_arg,
            tools=tools,
            messages=messages,
        )

        tool_call_count = sum(1 for b in response.content if b.type == "tool_use")
        session.turns.append(
            TurnStats(
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                tool_calls=tool_call_count,
            )
        )

        if response.stop_reason == "end_turn":
            session.final_answer = next(
                (b.text for b in response.content if hasattr(b, "text")), ""
            )
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = [
            {
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": execute_tool(block.name, block.input, repo_root, nav),
            }
            for block in response.content
            if block.type == "tool_use"
        ]
        messages.append({"role": "user", "content": tool_results})
    else:
        session.hit_turn_limit = True

    return session


def system_for(category: str, context_pack: str | None) -> str:
    base = CODING_SYSTEM if category == "coding" else QA_SYSTEM
    if context_pack:
        return base + CONTEXT_SUFFIX.format(context_pack=context_pack)
    return base


def parse_tasks(path: Path) -> list[tuple[str, str]]:
    tasks: list[tuple[str, str]] = []
    category = "explain"
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            m = re.match(r"#\s*CATEGORY:\s*(\w+)", stripped, re.IGNORECASE)
            if m:
                category = m.group(1).lower()
            continue
        tasks.append((stripped, category))
    return tasks


def _median(reps: list[SessionResult], fn) -> float:
    vals = [fn(s) for s in reps]
    return statistics.median(vals) if vals else 0.0


def aggregate_row(task: str, category: str, b_reps: list[SessionResult], t_reps: list[SessionResult]) -> dict:
    return {
        "task": task,
        "category": category,
        "b_in": _median(b_reps, lambda s: s.total_input),
        "t_in": _median(t_reps, lambda s: s.total_input),
        "b_out": _median(b_reps, lambda s: s.total_output),
        "t_out": _median(t_reps, lambda s: s.total_output),
        "b_tc": _median(b_reps, lambda s: s.total_tool_calls),
        "t_tc": _median(t_reps, lambda s: s.total_tool_calls),
        "b_turns": _median(b_reps, lambda s: s.total_turns),
        "t_turns": _median(t_reps, lambda s: s.total_turns),
        "hit": any(s.hit_turn_limit for s in (b_reps + t_reps)),
    }


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("No completed tasks.")
        return

    col_w = 40
    cat_w = 5
    header = (
        f"{'task':<{col_w}} {'cat':<{cat_w}} "
        f"{'b_in':>8} {'t_in':>8} {'Δin':>9}  "
        f"{'b_out':>6} {'t_out':>6} {'Δout':>7}  "
        f"{'b_tc':>4} {'t_tc':>4}"
    )
    sep = "─" * len(header)
    print(header)
    print(sep)

    tot = {k: 0.0 for k in ("b_in", "t_in", "b_out", "t_out", "b_tc", "t_tc", "b_turns", "t_turns")}
    for r in rows:
        label = r["task"][:col_w - 2] + ".." if len(r["task"]) > col_w else r["task"]
        d_in = r["t_in"] - r["b_in"]
        d_out = r["t_out"] - r["b_out"]
        marker = "!" if r["hit"] else " "
        print(
            f"{label:<{col_w}} {r['category'][:cat_w]:<{cat_w}}{marker}"
            f"{r['b_in']:>7,.0f} {r['t_in']:>8,.0f} {d_in:>+9,.0f}  "
            f"{r['b_out']:>6,.0f} {r['t_out']:>6,.0f} {d_out:>+7,.0f}  "
            f"{r['b_tc']:>4,.0f} {r['t_tc']:>4,.0f}"
        )
        for k in tot:
            tot[k] += r[k]

    print(sep)
    print(
        f"{'TOTAL':<{col_w}} {'':<{cat_w}} "
        f"{tot['b_in']:>7,.0f} {tot['t_in']:>8,.0f} {tot['t_in'] - tot['b_in']:>+9,.0f}  "
        f"{tot['b_out']:>6,.0f} {tot['t_out']:>6,.0f} {tot['t_out'] - tot['b_out']:>+7,.0f}  "
        f"{tot['b_tc']:>4,.0f} {tot['t_tc']:>4,.0f}"
    )
    print()

    grand_b = tot["b_in"] + tot["b_out"]
    grand_t = tot["t_in"] + tot["t_out"]
    net = grand_t - grand_b
    pct = (net / grand_b * 100) if grand_b else 0.0
    n = len(rows)
    print(f"Total tokens   — baseline: {grand_b:,.0f}   treatment: {grand_t:,.0f}   net: {net:+,.0f} ({pct:+.1f}%)")
    print(f"Avg turns      — baseline: {tot['b_turns'] / n:.1f}   treatment: {tot['t_turns'] / n:.1f}")
    print(f"Avg tool calls — baseline: {tot['b_tc'] / n:.1f}   treatment: {tot['t_tc'] / n:.1f}")
    print()

    print("By category (token totals, baseline → treatment):")
    cats: dict[str, dict] = {}
    for r in rows:
        c = cats.setdefault(r["category"], {"b": 0.0, "t": 0.0, "n": 0})
        c["b"] += r["b_in"] + r["b_out"]
        c["t"] += r["t_in"] + r["t_out"]
        c["n"] += 1
    for cat, c in cats.items():
        d = c["t"] - c["b"]
        p = (d / c["b"] * 100) if c["b"] else 0.0
        print(f"  {cat:<11} n={c['n']:<2} {c['b']:>10,.0f} → {c['t']:>10,.0f}   {d:>+11,.0f} ({p:+.1f}%)")
    print()
    print("Columns: b=baseline  t=treatment  tc=tool_calls (medians across reps)  != hit turn limit")


def serialize_session(s: SessionResult) -> dict:
    return {
        "condition": s.condition,
        "total_input_tokens": s.total_input,
        "total_output_tokens": s.total_output,
        "total_tool_calls": s.total_tool_calls,
        "total_turns": s.total_turns,
        "hit_turn_limit": s.hit_turn_limit,
        "turns": [
            {"input_tokens": t.input_tokens, "output_tokens": t.output_tokens, "tool_calls": t.tool_calls}
            for t in s.turns
        ],
        "final_answer": s.final_answer,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", required=True, help="Path to target repo (must have graph.json built via codex-graph map)")
    parser.add_argument("--tasks", required=True, help="Task file; '# CATEGORY: x' lines tag the tasks that follow")
    parser.add_argument("--model", default="claude-haiku-4-5-20251001", help="Anthropic model to use")
    parser.add_argument("--reps", type=int, default=1, help="Repetitions per (task, condition)")
    parser.add_argument("--max-turns", type=int, default=MAX_TURNS, dest="max_turns", help="Max agentic turns per session")
    parser.add_argument("--budget-usd", type=float, default=None, dest="budget_usd", help="Hard cost cap; stop before exceeding")
    parser.add_argument("--max-minutes", type=float, default=None, dest="max_minutes", help="Wall-clock time-box; stop before exceeding")
    parser.add_argument("--treatment", choices=["classic", "inline"], default="classic", help="Treatment arm: classic pack, or improved tiered pack + graph tools + caching")
    parser.add_argument("--json", action="store_true", dest="json_output", help="Emit JSON instead of a table")
    parser.add_argument("--out", help="Write/checkpoint JSON results to this path after every task (survives crashes)")
    args = parser.parse_args()

    repo_root = Path(args.root).resolve()
    tasks = parse_tasks(Path(args.tasks))
    if not tasks:
        sys.exit("No tasks found in tasks file.")

    rows: list[dict] = []
    json_rows: list[dict] = []
    spent = 0.0
    stopped_for_budget = False
    start_time = time.time()
    deadline = start_time + args.max_minutes * 60 if args.max_minutes else None

    def over_budget() -> bool:
        return args.budget_usd is not None and spent >= args.budget_usd

    def over_deadline() -> bool:
        return deadline is not None and time.time() >= deadline

    def should_stop() -> bool:
        return over_budget() or over_deadline()

    nav = None
    if args.treatment == "inline":
        pack_fn = get_context_pack_inline
        treatment_tools = TOOLS + GRAPH_TOOLS
        cache_treatment = True
        from codex_graph.multirepo import _overarching_graph_path
        from codex_graph.graph_nav import GraphNav
        gp = _overarching_graph_path(str(repo_root))
        if Path(gp).exists():
            nav = GraphNav(gp, CONTEXT_SKIP_PATTERNS)
    else:
        pack_fn = get_context_pack
        treatment_tools = TOOLS
        cache_treatment = False

    for i, (task, category) in enumerate(tasks, 1):
        if should_stop():
            stopped_for_budget = True
            why = f"spent ${spent:.2f}" if over_budget() else f"elapsed {(time.time()-start_time)/60:.0f}min"
            print(f"[stop] before task {i}: {why}", file=sys.stderr)
            break

        print(f"[{i}/{len(tasks)}] ({category}) {task[:64]}", file=sys.stderr)
        context_pack = pack_fn(task, repo_root)
        if not context_pack:
            print("  WARNING: empty context pack — is the graph built?", file=sys.stderr)
        baseline_system = system_for(category, None)
        treatment_system = system_for(category, context_pack)

        b_reps: list[SessionResult] = []
        t_reps: list[SessionResult] = []
        try:
            for rep in range(args.reps):
                if should_stop():
                    stopped_for_budget = True
                    break
                b = run_session(task, "baseline", baseline_system, repo_root, args.model, args.max_turns)
                spent += session_cost(b, args.model)
                b_reps.append(b)
                print(f"  baseline rep{rep+1}: {b.total_turns}t {b.total_tool_calls}tc {b.total_input + b.total_output:,}tok  (spent ${spent:.2f})", file=sys.stderr)

                if should_stop():
                    stopped_for_budget = True
                    break
                t = run_session(task, "treatment", treatment_system, repo_root, args.model, args.max_turns,
                                tools=treatment_tools, nav=nav, cache_system=cache_treatment)
                spent += session_cost(t, args.model)
                t_reps.append(t)
                print(f"  treatment rep{rep+1}: {t.total_turns}t {t.total_tool_calls}tc {t.total_input + t.total_output:,}tok  (spent ${spent:.2f})", file=sys.stderr)
        except Exception as exc:
            print(f"  ERROR on task {i}: {exc!r} — keeping partial reps", file=sys.stderr)

        if b_reps and t_reps:
            rows.append(aggregate_row(task, category, b_reps, t_reps))
            json_rows.append({
                "task": task,
                "category": category,
                "context_pack_chars": len(context_pack),
                "baseline_reps": [serialize_session(s) for s in b_reps],
                "treatment_reps": [serialize_session(s) for s in t_reps],
            })
            if args.out:
                Path(args.out).write_text(json.dumps(json_rows, indent=2))

        if stopped_for_budget:
            break

    print(f"\n[spend] total estimated cost: ${spent:.2f}" + (f" (cap ${args.budget_usd:.2f})" if args.budget_usd else ""), file=sys.stderr)
    if stopped_for_budget:
        print(f"[spend] run stopped early at budget cap after {len(rows)} completed tasks.", file=sys.stderr)

    if args.json_output:
        print(json.dumps(json_rows, indent=2))
    else:
        print()
        print_table(rows)


if __name__ == "__main__":
    main()
