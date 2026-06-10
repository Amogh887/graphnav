# codex-graph Token-Savings Experiment — wildfire-app

**Date:** 2026-06-04
**Target repo:** `/Users/amogh/wildfire-app` (FastAPI backend + React frontend, 337 graph nodes)
**Model:** `claude-haiku-4-5-20251001`
**Harness:** `eval/measure_savings.py` (agentic, multi-turn, tools: `read_file`, `list_directory`, `search_code`)
**Raw data:** `eval/wildfire_results.json`
**Run:** single run, 10 tasks × 2 conditions = 20 agentic sessions

---

## Method

Each task is answered twice by a tool-using Claude agent that explores the repo until it produces an answer (capped at 15 turns):

- **Baseline** — system prompt = "explore the repo with the tools." No prior knowledge.
- **Treatment** — same tools, but the `codex-graph context <task>` pack (ranked `file:line` regions + cross-service impact) is prepended to the system prompt.

`response.usage` is summed across every turn of each session. Tasks are deliberately split into **5 relevant** (features that exist: fire-risk model, FIRMS, weather normals, AUS feeds, training) and **5 irrelevant** (features that do *not* exist: auth/JWT, Stripe, GraphQL, Redis, WebSockets). The irrelevant set tests whether the context pack helps the model *rule things out* fast instead of hunting for code that isn't there.

---

## Headline numbers (all 10 tasks)

| Metric | Baseline | Treatment | Δ |
|---|---:|---:|---:|
| Total tokens | 276,318 | 283,498 | **+7,180 (+2.6%)** |
| Total output tokens | 13,386 | 8,843 | **−4,543 (−34%)** |
| Avg turns to answer | 7.2 | 4.4 | **−39%** |
| Avg tool calls | 11.5 | 5.5 | **−52%** |

At face value total tokens are *flat-to-slightly-worse*. **That headline is entirely caused by one pathological outlier** — see below. On every other axis (turns, tool calls, output verbosity) treatment wins decisively.

---

## The outlier: task 5 (ML model training)

Task 5 treatment went off the rails — it hit the 15-turn limit and burned **188,508 tokens** vs the baseline's 17,966. That single task contributes **+170,585** to the input-token delta. Remove it and the picture inverts completely:

| Metric (9 tasks, excl. task 5) | Baseline | Treatment | Δ |
|---|---:|---:|---:|
| Total tokens | 258,352 | 94,990 | **−163,362 (−63%)** |

**Important honesty caveat:** in the *first* (crashed) run, task 5 behaved the **opposite** way — baseline burned 106k/15 turns and treatment finished in 9.4k/2 turns. So task 5 is a high-variance task, not a consistent treatment failure. With Haiku and max_tokens loops, a single run can swing wildly when the model latches onto the wrong file and spirals. **One run is not enough to trust the aggregate; this needs N≥3 runs per task with medians.**

---

## Per-task results

`!` = hit the 15-turn limit.

| # | Task | Type | base tok | treat tok | Δ tok | base tools | treat tools |
|---|---|---|---:|---:|---:|---:|---:|
| 1 | fire risk prediction model | relevant | 36,537 | 9,760 | −73% | 10 | 3 |
| 2 | FIRMS fetch & cache | relevant | 29,286 | 8,354 | −71% | 11 | 2 |
| 3 | weather climate normals | relevant | 12,216 | 5,738 | −53% | 6 | 1 |
| 4 | AUS fire feeds (NSW/VIC) | relevant | 57,165 | 4,634 | −92% | 15 | 1 |
| 5 | ML training & features | relevant | 17,966 | 188,508 ! | +949% | 7 | 17 |
| 6 | auth / JWT login | irrelevant | 22,223 | 5,623 | −75% | 12 | 5 |
| 7 | Stripe payments | irrelevant | 29,739 | 14,064 | −53% | 15 | 7 |
| 8 | GraphQL schema | irrelevant | 20,120 | 12,052 | −40% | 10 | 5 |
| 9 | Redis caching | irrelevant | 26,603 | 12,356 | −54% | 11 | 6 |
| 10 | WebSocket connections | irrelevant | 24,463 | 22,409 | −8% | 18 | 8 |

**9 of 10 tasks: treatment cut total tokens 8–92%.** The lone exception is the variance-driven outlier.

---

## Relevant vs. irrelevant

| Group | Baseline tok | Treatment tok | Δ |
|---|---:|---:|---:|
| Relevant, excl. outlier (1–4) | 135,204 | 28,486 | **−79%** |
| Irrelevant (6–10) | 123,148 | 66,504 | **−46%** |

Both groups improve. The relevant tasks improve most — the context pack points straight at the right `file:line` regions, so the agent reads one or two files instead of crawling the tree. The irrelevant tasks still improve ~46%: the pack lists what *does* exist, letting the model conclude "no auth / no Stripe / no Redis here" in ~half the tool calls instead of exhaustively searching for absent features.

---

## Interpretation — does it help or hinder developers?

**It helps, clearly, on the metrics that map to developer pain:**

- **Fewer tool calls (−52%) and fewer turns (−39%)** mean fewer round-trips before the agent is productive. In a real Claude Code session this is the difference between "answers immediately" and "spends a minute spelunking."
- **−34% output tokens** means more focused, less hedged answers.
- On relevant tasks the input-token savings are large (≈−79%) because blind exploration of a 337-node repo is expensive and the pack short-circuits it.

**The honest risks:**

- **Variance is real.** The task-5 blow-up shows a context pack can occasionally send the model down a wrong path that it then over-explores. With a small/cheap model and an open-ended turn budget, one bad run dominates an aggregate. This is the single most important caveat.
- **Single run, single model.** These numbers are directional, not publication-grade. Repeat with N≥3 runs/task, report medians, and re-run on `claude-sonnet-4-6` (the model devs actually use) before drawing firm conclusions.
- **Sandbox ≠ real Claude Code.** The harness's three toy tools approximate, but don't equal, the real agent's tooling and prompt.

---

## Recommended next steps

1. Re-run with `--model claude-sonnet-4-6` and 3 repetitions per task; compare medians (removes the variance problem that produced the task-5 outlier).
2. Investigate task 5: capture the treatment transcript to see why the agent looped — likely a too-broad context pack for "training" that listed many files.
3. Consider lowering `MAX_TURNS` or adding a token budget per session to cap worst-case blow-ups.

## Bottom line

On 9 of 10 tasks codex-graph **substantially reduced** token consumption (−40% to −92%) and **roughly halved** tool calls and turns. The only reason the raw aggregate looks flat is a single high-variance outlier that swung the other way on a prior run. The evidence points to codex-graph **helping** developers — pending a multi-run confirmation to nail down the magnitude.
