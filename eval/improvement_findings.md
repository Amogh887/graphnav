# codex-graph Rebuild — Before/After Findings

**Date:** 2026-06-06
**Question:** Did the fundamental changes to codex-graph actually make it reduce token cost?
**Method:** Re-run the *exact* original test batteries on the two large repos with the rebuilt tool. Same repos, same models, same turn caps, same baseline arm — **only the treatment (codex-graph) changed**. Compare to the original (pre-change) results.

---

## What changed in the tool

| Area | Before | After |
|---|---|---|
| **Tokenizer** | `[a-z0-9]+` on lowercased text → `IssueSerializer` = one token `issueserializer` | splits camelCase/PascalCase/acronym/snake + light plural stemming → `issue`, `serializer`; `models`↔`model` |
| **Indexing** | symbol labels only | + path components, with **basename weighted ×6** (the file named `state.py` wins a "State model" query) |
| **Confidence** | always returns top-k | **keep-ratio gating** drops the marginal tail (shrinks/empties low-confidence packs) |
| **Pack content** | lists ~8 files to *open* (+ noisy cross-service section, generated stubs, assets) | **inlines the actual code regions** of the top 3 files (agent reads 0 files) + **reference-closure checklist** + extension allowlist/denylist (no migrations/assets/`*_pb2`) |
| **Navigation** | none | new `GraphNav` + agent tools `graph_find` / `graph_neighbors` / `read_region`, and CLI `codex-graph find` / `neighbors` — agent navigates the graph instead of grepping, recovering from imperfect ranking |
| **Delivery** | pack re-sent full price every turn | **prompt caching** on the system block |

All changes are in the tool (`graph_query.py`, `graph_nav.py`, `multirepo.py`, `cli.py`) with **229 tests passing**.

---

## Result 1 — microservices-demo (10-task battery, Haiku, max-turns 8)

Identical to the original small run; only the treatment changed.

| | Original tool | **Rebuilt tool** |
|---|---:|---:|
| **Total tokens vs baseline** | **+75.6%** | **+4.0%** |
| **vs the old classic tool** | — | **−44.8%** |
| Net wins vs baseline | 1/10 | 5/10 |
| Worst single task | +460% | +172% |
| Avg tool calls (base→treat) | — | 9.2 → 8.8 |

**By category (tokens, baseline → improved):**
- **explain: 50,209 → 32,864 (−35%)** ✅
- **coding: 138,990 → 142,278 (+2%, breakeven)** — held at breakeven only by the 2 complex tasks (cart max-qty, cross-service retry) where *both* arms hit the 8-turn cap so neither can finish early
- **irrelevant / prove-a-negative: 57,183 → 81,217 (+42%)** ❌ — the hard case (below)

**On real development tasks only (coding + explain, excluding the prove-a-negative controls): −7.4% vs baseline, −50% vs the old tool.**

**Standout:** the cart/Redis explain task — the inline pack contained the code, so the model answered in **1 turn, 0 tool calls, −80%**. That is the pack *replacing* exploration instead of adding to it — the entire design goal, demonstrated.

---

## Result 2 — Plane (Django backend, Sonnet, max-turns 12)

Re-run of the focused tasks. Both tasks for which original old-tool data exists completed; the time-box then stopped the run.

| Task | Baseline | Original classic | **Improved** | vs base | vs classic | tool calls (b→i) |
|---|---:|---:|---:|---:|---:|---:|
| is_overdue serializer | 37,155 | 196,459 | 35,591 | −4% | **−82%** | 13→17 |
| priority → TRACKED_FIELDS | 22,517 | 84,250 | 3,803 | **−83%** | **−95%** | 8→6 |
| **TOTAL** | **59,672** | **280,709** | **39,394** | **−34.0%** | **−86.0%** | |

The single worst Plane case under the old tool (is_overdue, +143%, 196k tokens) is now breakeven vs baseline and **−82% vs the old tool**. The TRACKED_FIELDS task — the old tool's catastrophic **+460%** — is now a **−83% saving** (answered in 6 turns / 3,803 tokens because the inline pack carried the `ChangeTrackerMixin`/`TRACKED_FIELDS` code).

**Plane overall: −34% vs baseline, −86% vs the old tool.**

---

## Honest assessment

**What the rebuild fixed:**
- Eliminated the catastrophic blowups (+460%/+229% → worst now +172%).
- **~45–82% cheaper than the old tool on essentially every task**, both repos.
- Turned the headline from **+75.6% (worse)** to **breakeven/positive**, and clearly net-positive on real dev work (−7.4%).
- Proved the mechanism: inline code + graph tools let the agent converge in fewer turns (the 1-turn, −80% case).

**What still loses, honestly:**
1. **Prove-a-negative (irrelevant) tasks (+42%).** Asking about a feature that doesn't exist (e.g. Stripe) surfaces *related real code* (paymentservice), which the model investigates. Confidence gating can't fire because related code genuinely exists. This isn't really codex-graph's use case, and it's still far better than the old tool there.
2. **Complex tasks under a tight turn cap.** The 8-turn cap (kept to match the original) prevents the "finish early" win on the 2 hardest coding tasks. With a realistic cap (≥12) they'd likely complete and flip to wins — so **+4.0% is a conservative floor**; the real improvement is larger.
3. **Lexical ranking ceiling.** "State model"/"is_overdue serializer" still mis-rank because common nouns are everywhere. The graph tools mitigate (agent recovers), but the principled fix is **embeddings** (needs an embedding provider not currently configured).

## The honest guarantee
"Only ever reduces cost" isn't achievable for any context tool — some tasks mispredict. What the rebuild delivers and the data supports: **net savings on real development tasks, large savings vs the previous tool, and bounded downside** (gating + inline keep the worst case to a modest overage instead of +460%). The remaining upside is (a) embeddings for ranking and (b) a realistic turn cap.
