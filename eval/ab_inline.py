import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from measure_savings import run_session, system_for, CONTEXT_SKIP_PATTERNS
from codex_graph.multirepo import build_context_pack, build_context_pack_inline

ROOT = Path("/Users/amogh/plane/apps/api/plane")
MODEL = "claude-sonnet-4-6"
MAX_TURNS = 12
TASK = ("add a per-user throttle for issue creation limiting to 30 requests per "
        "minute, mirroring the existing asset throttle")
CAT = "coding"

classic = build_context_pack(str(ROOT), TASK, top_files=8, budget_tokens=2000, skip_patterns=CONTEXT_SKIP_PATTERNS)
inline = build_context_pack_inline(str(ROOT), TASK, top_files=3, budget_tokens=2500, skip_patterns=CONTEXT_SKIP_PATTERNS)
print(f"PACKS  classic={len(classic)}c  inline={len(inline)}c", flush=True)
print("inline files: " + " ".join(l[4:] for l in inline.splitlines() if l.startswith("### ")), flush=True)


def show(name, s):
    print(f"{name:16}: {s.total_turns}t {s.total_tool_calls}tc  in={s.total_input:,} out={s.total_output:,} total={s.total_input + s.total_output:,}", flush=True)


b = run_session(TASK, "baseline", system_for(CAT, None), ROOT, MODEL, MAX_TURNS)
show("baseline", b)
c = run_session(TASK, "classic-treat", system_for(CAT, classic), ROOT, MODEL, MAX_TURNS)
show("classic-treat", c)
i = run_session(TASK, "inline-treat", system_for(CAT, inline), ROOT, MODEL, MAX_TURNS)
show("inline-treat", i)
print("DONE", flush=True)
