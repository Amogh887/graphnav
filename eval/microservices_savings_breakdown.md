# Where the Tokens Went — microservices-demo (mechanism deep-dive)

Companion to `microservices_findings.md`. The headline was: treatment cost **+75.6%** overall (9/10 tasks worse). This explains *why*, turn by turn, from `microservices_results_small.json`.

---

## 1. Input is the whole story (again)

| | Baseline | Treatment |
|---|---:|---:|
| Input tokens | 252,714 (95.6%) | 453,100 |
| Output tokens | 11,571 | 10,927 |

As on wildfire, input is ~96% of cost. The difference: here input went **up** +79%. So the question is "why did input balloon under treatment," and the answer is the context pack — both its weight and the reading it provoked.

---

## 2. The per-turn pack tax, shown directly

The context pack lives in the system prompt and is **re-sent on every turn**. Compare per-turn input for **task 2 (shipping free-shipping)**, pack = 8,025 chars (~2k tokens):

| turn | baseline in | treatment in |
|---:|---:|---:|
| 1 | 857 | 3,644 |
| 2 | 1,035 | 3,967 |
| 3 | 1,168 | 7,189 |
| 4 | 3,474 | 7,294 |
| 5 | 4,522 | 10,346 |
| 6 | 7,589 | 10,487 |
| 7 | 7,729 | 13,539 |
| 8 | 10,781 | 15,271 |
| **sum** | **37,155** | **71,737** |

- **Turn 1 gap = 3,644 − 857 = 2,787 tokens** ≈ the pack itself (plus a slightly larger first read it triggered).
- That gap **persists and grows** every turn, because the pack rides along *and* the agent reads the (often large, generated) files the pack named.
- Crucially, **treatment took the same 8 turns** — it never escaped early, so the tax had nothing to offset it.

This is exactly the inverse of wildfire, where treatment took 2 turns and the per-turn tax was paid only twice while baseline paid its growing history 8–10 times.

---

## 3. The smoking gun: the only win had no pack

**Task 10 (Kafka, absent feature)** — codex-graph found nothing relevant and returned a **435-char (≈150-token) pack**:

| turn | baseline in | treatment in |
|---:|---:|---:|
| 1 | 790 | 944 |
| … | … | … |
| 8 | 6,350 | 3,356 |
| **sum** | **27,460** | **14,948** |

- Turn-1 gap is just **154 tokens** (the tiny pack).
- With almost no pack weight, treatment actually stayed **leaner** and finished at **14,948 vs 27,460 (−46%)**.

So when the pack is near-empty, treatment behaves like (or beats) baseline. When the pack is full (~2k tokens), treatment loses. **The pack's cost outweighs its benefit on this repo.**

---

## 4. Turns and tool calls didn't move — the benefit mechanism never fired

| | Baseline | Treatment |
|---|---:|---:|
| Avg turns | 7.4 | 7.3 |
| Avg tool calls | 9.1 | 9.5 |

codex-graph's entire value is *shortening the agent's path*. Here it didn't: the agent explored just as much (slightly more tool calls). Reasons:
- **Repo scale**: 12 services / 1,610 nodes — the pack's "open only these files" list still leaves a large surface, and the files it names (generated gRPC stubs, assets) aren't the ones that answer the task.
- **Pack precision**: BM25 on a generated-code-heavy repo surfaces `demo_grpc.pb.go` / `demo_pb2_grpc.py` and product `.jpg`s. Reading those burns tokens without progressing.
- **Turn cap**: at max-turns 8, hard tasks never reach a confident stop in either arm (5/10 baseline, 4/10 treatment produced an answer), so "finish early" — the win condition — couldn't happen.

---

## 5. Output barely changed, and quality didn't improve

Output: 11,571 → 10,927 (−5.6%), negligible. Among the 2 coding tasks that finished in both arms, the emitted diffs were **similar length** (payment: 4,992 vs 4,829 chars; currency: 3,711 vs 2,922) — treatment's large extra input cost bought **no better output**. On `auth/JWT`, treatment was actually *worse on the outcome*: it hit the turn cap with no answer while baseline finished and correctly reported "no auth here."

---

## 6. Two methodological factors that inflate (but don't reverse) the penalty

- **No prompt caching.** The pack is billed at full input price every turn. With caching (real Claude Code), repeat-turn pack cost drops ~90%. That would shrink the per-turn tax a lot — but since turns weren't reduced, treatment would likely still be ≥ baseline, not below it.
- **Turn cap 8.** A higher cap would let treatment finish some tasks, potentially recovering the wildfire-style win on the simpler ones. Both factors were forced by the API tier (5 req/min, 10k ITPM), not chosen for the science.

---

## 7. Bottom line

Tokens went **up**, entirely in **input**, because a ~2k-token context pack was **re-sent every turn and provoked extra reading** while **failing to shorten the agent's path** (turns flat). The single near-empty pack (Kafka) was the single win — proof that on this repo the pack's cost exceeds its benefit. codex-graph helps when its pack collapses exploration (wildfire); it hurts when the pack is heavy/noisy and exploration doesn't shrink (here). A fair re-test needs a higher API tier (turn cap ≥15), prompt caching, multiple reps on Sonnet, and tighter packs (exclude generated/asset files from the graph).
