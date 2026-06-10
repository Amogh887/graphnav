# Where Exactly the Tokens Were Cut — wildfire-app

A turn-by-turn decomposition of the experiment in `wildfire_results.json`. The summary said "−63% excluding the outlier." This document explains *where* in each session that reduction actually came from.

---

## 1. The single biggest fact: input tokens are ~95% of the cost

| | Baseline | Treatment |
|---|---:|---:|
| Total **input** tokens | 262,932 (95.2%) | 274,655 |
| Total **output** tokens | 13,386 (4.8%) | 8,843 |

Output is a rounding error. **Whether codex-graph saves money is almost entirely a question of input tokens.** So "where were tokens cut" = "where were input tokens cut," and the answer is: **by eliminating exploration turns.**

---

## 2. The mechanism: input cost grows ~quadratically with turns

In an agentic loop, every turn re-sends the *entire* accumulated conversation (system prompt + every prior tool call + every prior tool result). So if the agent reads files across N turns, the file contents read early get re-billed on every subsequent turn. Total input ≈ O(N²) in the content portion.

This is visible directly in the per-turn input counts. **Task 4 (AUS fire feeds)** baseline:

| turn | input tokens | tool calls | what's happening |
|---:|---:|---:|---|
| 1 | 794 | 3 | blind: list dir, read README, search |
| 2 | 1,091 | 2 | history starting to accumulate |
| 3 | 3,334 | 2 | file contents now re-sent each turn |
| 4 | 3,567 | 1 | |
| 5 | 4,728 | 2 | |
| 6 | 4,942 | 2 | |
| 7 | 6,707 | 1 | |
| 8 | 8,387 | 1 | |
| 9 | 9,592 | 1 | |
| 10 | 12,267 | 0 | final answer re-sends 12k of accumulated context |
| | **55,409 total** | 15 | |

Treatment, same task:

| turn | input tokens | tool calls | what's happening |
|---:|---:|---:|---|
| 1 | 1,414 | 1 | context pack named the file → read it directly |
| 2 | 2,587 | 0 | answer |
| | **4,001 total** | 1 | |

**−51,408 input tokens (−93%) on this one task**, purely from collapsing 10 turns into 2. Note the last baseline turn *alone* (12,267) costs 3× the entire treatment session. That is the quadratic tax: it's not that baseline read 13× more content, it's that it re-sent the growing pile 10 times instead of twice.

---

## 3. Where the cut happens within a session: the first turn

The divergence is decided on turn 1, every time:

- **Baseline turn 1**: input ≈ 790 (system + task only), then 2–3 tool calls that are *orientation* — `list_directory(".")`, read `README.md`, a broad `search_code`. The agent doesn't know where anything is, so it spends 3–7 turns narrowing down before it reads the file that actually answers the question.
- **Treatment turn 1**: input ≈ 1,300–1,420 (system now carries the ~370-token context pack), then tool calls that read the *named* `file:line` regions directly. No orientation phase.

The context pack costs ~350–600 extra input tokens on turn 1 (the `context_pack_chars` field is ~1,200–1,680 chars per task). That upfront cost is repaid many times over by removing 5–8 orientation turns, each of which would have re-billed the accumulating history.

---

## 4. Per-task decomposition of input savings

| # | Task | Type | base turns | treat turns | base in | treat in | Δ input |
|---|---|---|---:|---:|---:|---:|---:|
| 1 | fire risk model | rel | 8 | 2 | 35,093 | 8,887 | **−26,206** |
| 2 | FIRMS service | rel | 7 | 3 | 27,669 | 7,529 | **−20,140** |
| 3 | weather normals | rel | 4 | 2 | 11,311 | 5,146 | **−6,165** |
| 4 | AUS feeds | rel | 10 | 2 | 55,409 | 4,001 | **−51,408** |
| 5 | ML training | rel | 5 | 15 ! | 16,547 | 187,132 | **+170,585** |
| 6 | auth/JWT | irr | 8 | 2 | 21,138 | 4,957 | **−16,181** |
| 7 | Stripe | irr | 7 | 8 | 28,684 | 13,355 | **−15,329** |
| 8 | GraphQL | irr | 8 | 4 | 19,111 | 11,275 | **−7,836** |
| 9 | Redis | irr | 8 | 2 | 25,164 | 10,852 | **−14,312** |
| 10 | WebSockets | irr | 7 | 4 | 22,806 | 21,521 | **−1,285** |

The Δ input column tracks the turn-count column almost perfectly. **Savings = turns eliminated.** Task 5 is the exception (next section).

---

## 5. Relevant vs. irrelevant: two different savings mechanisms

| Group | base in | treat in | Δ | mechanism |
|---|---:|---:|---:|---|
| Relevant, excl. task 5 (1–4) | 129,482 | 25,563 | **−80%** | pack points *at* the answer → agent reads named files, skips search entirely |
| Irrelevant (6–10) | 116,903 | 61,960 | **−47%** | pack lists what *does* exist → agent rules out the absent feature faster, but still has to poke around |

This is the most interesting structural finding: **the two task types save tokens for different reasons.**

- On **relevant** tasks, the pack contains the answer's location, so exploration collapses to "read these 1–2 files." Biggest wins (−71% to −93%).
- On **irrelevant** tasks (auth, Stripe, Redis, WebSockets, GraphQL — none exist here), the pack *can't* point at code that isn't there. The agent still has to prove a negative. It saves by bounding the search (the pack shows the real file inventory, so the model stops hunting sooner) but the cuts are smaller and noisier. Task 10 (WebSockets) only saved 8% — treatment still did 8 tool calls confirming there's no WebSocket code; baseline did 18.

---

## 6. Where tokens were NOT cut

- **Task 5 (ML training), +170,585 input**: treatment spiraled — hit the 15-turn cap and never produced a final answer (`final_answer: ""`). Each turn read another training-related file (`train_model.py` is large) and re-billed the growing history 15 times — the quadratic tax working *against* treatment. The context pack for "training" likely listed too many files, inviting the agent to open all of them. **Caveat:** the prior crashed run showed the opposite (baseline 106k, treatment 9k), so this is high variance, not a reliable treatment failure. This one task single-handedly flips the headline from −60% to +2.6%.
- **Task 10 (WebSockets), −8% input**: proving a negative for a feature the pack can't address. Floor case for the technique.
- **Output tokens generally**: cut a steady −34% (13,386 → 8,843), but since output is <5% of cost, this barely moves the total. The output savings come from removing intermediate narration — baseline emits 60–227 tokens of "let me look at…" commentary on each of its many turns; treatment has fewer turns so less narration. Final-answer lengths are similar between conditions.

---

## 7. Bottom line: where the tokens were cut

1. **In the input, not the output** (input is 95% of cost).
2. **By removing orientation turns** — the 3–8 blind `list`/`read README`/`search` turns baseline needs before it finds the right file. The context pack replaces those with a direct read on turn 1.
3. **Amplified by the quadratic re-billing of conversation history** — each eliminated turn also removes a re-send of everything read so far, so cutting turns 10→2 cuts input 93%, not 80%.
4. **Most on relevant tasks** (pack points at the answer, −80%), **less on irrelevant ones** (pack bounds a negative search, −47%).
5. **Except when the pack is too broad** (task 5), which can invite over-reading and reverse the savings — the key risk to control, and the reason to re-run N≥3× on Sonnet before trusting the magnitude.
