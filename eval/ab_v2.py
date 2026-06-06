import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from measure_savings import run_session, system_for, TOOLS, GRAPH_TOOLS, CONTEXT_SKIP_PATTERNS
from codex_graph.multirepo import build_context_pack, build_context_pack_inline, _overarching_graph_path
from codex_graph.graph_nav import GraphNav

ROOT = Path("/Users/amogh/plane/apps/api/plane")
MODEL = "claude-sonnet-4-6"
MAX_TURNS = 12
CAT = "coding"
TASKS = [
    "add a per-user throttle for issue creation limiting to 30 requests per minute, mirroring the existing asset throttle",
    "add a validator to the workspace serializer that rejects a slug containing spaces or uppercase letters",
]

graph_path = _overarching_graph_path(str(ROOT))
nav = GraphNav(graph_path, CONTEXT_SKIP_PATTERNS)


def show(name, s):
    print(f"  {name:16}: {s.total_turns}t {s.total_tool_calls}tc  in={s.total_input:,} out={s.total_output:,} total={s.total_input + s.total_output:,}", flush=True)


for task in TASKS:
    print(f"\nTASK: {task[:64]}", flush=True)
    classic = build_context_pack(str(ROOT), task, top_files=8, budget_tokens=2000, skip_patterns=CONTEXT_SKIP_PATTERNS)
    tiered = build_context_pack_inline(str(ROOT), task, top_files=3, budget_tokens=2500, skip_patterns=CONTEXT_SKIP_PATTERNS)
    print(f"  PACKS classic={len(classic)}c tiered={len(tiered)}c", flush=True)

    b = run_session(task, "baseline", system_for(CAT, None), ROOT, MODEL, MAX_TURNS, tools=TOOLS)
    show("baseline", b)
    c = run_session(task, "classic-treat", system_for(CAT, classic), ROOT, MODEL, MAX_TURNS, tools=TOOLS, cache_system=True)
    show("classic-treat", c)
    v = run_session(task, "improved-v2", system_for(CAT, tiered), ROOT, MODEL, MAX_TURNS, tools=TOOLS + GRAPH_TOOLS, nav=nav, cache_system=True)
    show("improved-v2", v)
print("\nDONE", flush=True)
