# codex-graph Token-Savings Experiment — microservices-demo (Online Boutique)

**Date:** 2026-06-05
**Target repo:** `GoogleCloudPlatform/microservices-demo` @ tag `v0`, rooted at `/src` (12 polyglot services: Go, C#, Node, Python, Java; gRPC)
**Graph:** 1,610 nodes / 3,841 links, with real cross-service bridges (`checkoutservice → cart/currency/email/productcatalog/shipping`)
**Model:** `claude-haiku-4-5-20251001` · **1 rep** · **max-turns 8**
**Harness:** `eval/measure_savings.py` (read-only tools; coding tasks emit a unified diff)
**Raw data:** `eval/microservices_results_small.json` · **Spend:** $0.82

> ⚠️ This is a **reduced run** (see "Why this is a small run" and "Caveats"). The result is directionally clear but confound-heavy. Do not over-generalize from it.

---

## Headline: on this repo, codex-graph made things *worse*

| Metric | Baseline | Treatment | Δ |
|---|---:|---:|---:|
| Total tokens | 264,285 | 464,027 | **+199,742 (+75.6%)** |
| Input tokens | 252,714 | 453,100 | **+79.3%** |
| Output tokens | 11,571 | 10,927 | −5.6% |
| Avg turns | 7.4 | 7.3 | ~flat |
| Avg tool calls | 9.1 | 9.5 | ~flat |
| **Tasks treatment was cheaper** | — | — | **1 / 10** |

This is the **opposite** of the wildfire-app result (where treatment cut tokens ~40–90% by finishing in far fewer turns). Here treatment cost **more on 9 of 10 tasks**, across every category.

---

## Why it reversed: the pack didn't reduce turns, so its weight was pure overhead

In wildfire, codex-graph won because the context pack let the agent finish in 2 turns instead of 8–10. **Here, turns and tool calls were essentially unchanged** (7.3 vs 7.4 turns; 9.5 vs 9.1 tool calls). The pack did not help the agent converge faster on this large repo — so its only measurable effect was added cost:

1. **Per-turn pack tax.** The context pack (~7,600–8,025 chars ≈ ~2,000 tokens for 9/10 tasks) is carried in the system prompt and **re-sent every turn**. Over ~7–8 turns that alone is ~15k extra input tokens per task.
2. **The pack induced *more* reading, not less.** It frequently pointed at large generated gRPC stubs (`demo_grpc.pb.go`, `demo_pb2_grpc.py`) and even static assets (product `.jpg` files), which the agent dutifully opened — inflating input further.
3. **The smoking gun:** the only task where treatment won (**Kafka**, irrelevant) is the only one where codex-graph returned a near-empty pack (435 chars) because nothing matched. No pack → no tax → treatment cheaper (16k vs 28k). Every task with a full ~2k-token pack lost.

Input is the entire story (+79% input; output flat). This mirrors wildfire's "input dominates" finding — but here the input moved the wrong way.

---

## Per-task results

`cap` = hit the 8-turn limit. `ans` = produced a final answer/diff.

| # | Task | Cat | base tok | treat tok | Δ | b ans | t ans | pack chars |
|---|---|---|---:|---:|---:|:--:|:--:|---:|
| 1 | cart max-quantity | coding | 12,148 | 31,364 | +158% | ✗cap | ✗cap | 7,616 |
| 2 | shipping free-shipping | coding | 37,867 | 72,475 | +91% | ✗cap | ✗cap | 8,025 |
| 3 | payment expired-card | coding | 26,433 | 43,411 | +64% | ✓ | ✓ | 7,576 |
| 4 | currency cache TTL | coding | 21,455 | 52,076 | +143% | ✓ | ✗cap | 8,025 |
| 5 | frontend→checkout retry (cross-svc) | coding | 37,608 | 61,675 | +64% | ✗cap | ✗cap | 7,717 |
| 6 | checkout orchestration | explain | 37,472 | 67,836 | +81% | ✗cap | ✗cap | 8,025 |
| 7 | cart Redis backing | explain | 14,675 | 21,924 | +49% | ✓ | ✓ | 7,616 |
| 8 | auth/JWT (absent) | irrelevant | 39,949 | 69,681 | +74% | ✓ | ✗cap | 8,025 |
| 9 | Stripe refunds (absent) | irrelevant | 8,445 | 27,804 | +229% | ✓ | ✓ | 8,025 |
| 10 | Kafka bus (absent) | irrelevant | 28,233 | 15,781 | **−44%** | ✗cap | ✗cap | **435** |

### By category
| Category | n | base tok | treat tok | Δ |
|---|---:|---:|---:|---:|
| coding | 5 | 135,511 | 261,001 | **+92.6%** |
| explain | 2 | 52,147 | 89,760 | **+72.1%** |
| irrelevant | 3 | 76,627 | 113,266 | **+47.8%** |

Coding (the developer-viability category) was hit *hardest* — the heaviest packs, the most induced reading, and (with the 8-turn cap) the least chance to "finish early."

---

## A big confound: half the sessions never finished

With the rate-limit-forced **8-turn cap**, many sessions ran out of turns mid-exploration and produced **no answer/diff at all — in both conditions**:

- **Final answer produced: baseline 5/10, treatment 4/10.**
- 3 of 5 coding tasks were EMPTY in both arms (cart max-qty, shipping, retry).
- The 2 coding tasks that did finish (payment validation, currency TTL) produced diffs of **similar length in both conditions** — i.e., treatment's extra tokens bought **no quality improvement**.
- One stark case: **auth/JWT** — baseline finished and correctly concluded "no auth here," while **treatment hit the cap with no answer** because the pack lured it into extra exploration. The pack actively hurt.

So part of the coding comparison is "both arms failed to finish, and treatment cost more to fail." That's a real artifact of the low turn cap, which existed only because of the API tier (see below).

---

## Why this is a small run

The org's API tier caps at **5 req/min, 10k input tok/min, 4k output tok/min**. The originally-planned run (both models, 3→1 reps, 30 tasks) would have taken 12–18 h of throttled grinding. We cut to **Haiku, 10 tasks, max-turns 8** to finish in ~1.5 h. The $23 budget was never the binding constraint — the rate limit was. Actual spend: **$0.82**.

---

## Caveats (read before trusting the magnitude)

1. **Turn cap too low.** max-turns 8 caused non-completion in both arms and removed the "finish early" mechanism that is codex-graph's whole advantage. A fair test needs ≥15 turns — which needs a higher API tier.
2. **No prompt caching.** The harness re-bills the context pack at full input price every turn. Real Claude Code caches the system prompt, so a cached pack would cost ~10% on repeat turns — shrinking treatment's penalty substantially (though, since turns weren't reduced, likely not enough to flip the sign).
3. **Small N, single rep, single model (Haiku).** No medians, no variance control, no cross-model check. Individual task deltas are noisy.
4. **Noisy packs from generated code.** codex-graph's ranking surfaced generated gRPC stubs and image assets on this repo. That is real tool behavior on a codebase full of generated artifacts, but it disproportionately hurt here.

---

## Bottom line

On this large, polyglot, generated-code-heavy repo, **codex-graph did not help and usually hurt** — it raised token usage ~76% overall (and ~93% on coding tasks) without reducing turns, tool calls, or improving answer quality. The mechanism is clear and consistent: the ~2k-token pack is re-sent every turn and induces extra file reading, while failing to make the agent converge faster (the one tiny-pack task was the only win).

This contrasts sharply with wildfire-app, where the pack *did* collapse exploration and saved 40–90%. **The deciding factor is whether the context pack actually shortens the agent's path.** It did on a small focused repo; it did not on this large one with noisy packs — and the low turn cap (forced by the API tier) further suppressed any upside.

**Recommended before drawing a firm verdict:** raise the API tier, then re-run with max-turns ≥15, prompt caching enabled, ≥3 reps, on Sonnet — and consider excluding generated/asset files from the graph so packs are tighter. See `eval/microservices_savings_breakdown.md` for the per-turn mechanism detail.
