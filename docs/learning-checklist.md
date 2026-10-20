# Learning Checklist (post-build review)

Personal study guide for after the build finishes. The goal: be able to **explain and defend every part of this project**, not just demo it.

How to use it: work top to bottom, check items off, and write short notes in your own words. Anything you can't explain is a gap to close before an interview.

---

## 1. Read the code in the right order

Be able to explain each piece in your own words.

- [ ] `fixtures/<scenario>/scenario.yaml` and a slice of `events.jsonl`: what does the data look like?
- [ ] One tool implementation, then the MCP server wiring
- [ ] `agent/loop.py`: trace one full iteration by hand. If you can't, ask for it to be simplified.
- [ ] The system prompt: which lines are load-bearing?
- [ ] `evals/scoring.py`: what counts as a pass, and what could slip through?

---

## 2. Watch the agent work

- [ ] Run `investigate` on at least 3 scenarios
- [ ] Read the step logs line by line: tool, arguments, result, next decision
- [ ] For each step, ask: "Would I have done that?" Note where the agent differs from you.
- [ ] Read **at least 3 failed transcripts** in full

---

## 3. Break things on purpose (and record the numbers)

For each, run the evals before and after and note the change in score, steps, tokens, and cost.

| Experiment | What it teaches | Result |
|---|---|---|
| Make a tool description vague | Descriptions are prompts | |
| Remove result truncation | Context is a budget | |
| Remove `find_outliers` | Why BubbleUp-style tooling matters | |
| Change the step limit | Behavior at the edges | |
| Swap the model (smaller/larger, or Bedrock vs. direct API) | Quality vs. cost vs. latency | |
| Shorten the system prompt | What's actually load-bearing | |
| Disable compaction | Value of context management | |
| Disable memory | Whether memory helps at all | |

---

## 4. Stress the evals themselves

- [ ] Are the scenarios too easy? If everything passes on run one, add harder ones.
- [ ] Try to game the scoring: can the agent pass without finding the real cause?
- [ ] Run one scenario 10 times and measure variance
- [ ] Hand-check the anti-fabrication check on several runs
- [ ] Confirm ground truth: was the enabled fault flag really the cause?

---

## 5. Security and failure behavior

- [ ] Read what the agent actually did in the prompt-injection scenario (not only the score)
- [ ] Write your own injection attempt into a fixture attribute and test it
- [ ] Seed a stale or wrong memory: does the agent verify it or trust it?
- [ ] Make a tool error or time out: does the agent recover sensibly?
- [ ] Check that no secrets are in git history

---

## 6. Learn the surrounding concepts hands-on

- [ ] Use the MCP server from an external client (Claude Code or MCP Inspector). How does it differ from your own harness?
- [ ] **Investigate one fault yourself first** in a UI, then compare your path with the agent's
- [ ] Get comfortable with: p50/p95/p99, a latency heatmap, a trace tree, wide events, high cardinality
- [ ] Check tokens and cost per investigation, then estimate cost for 1,000 investigations
- [ ] Compare local DuckDB queries against the same questions on Honeycomb (if configured)

---

## 7. Questions to answer without notes

- [ ] Why these tools, and what did you cut?
- [ ] What did the evals catch that you wouldn't have noticed?
- [ ] What was the biggest improvement? Which change did *not* help?
- [ ] Where does it still fail, and why?
- [ ] What breaks first at production scale? (auth, tenancy, rate limits, cost, injection)
- [ ] When would you *not* use MCP, or not use an agent, for this problem?
- [ ] Why does a fast, high-cardinality store change what an agent can reason about?
- [ ] What would you build next, and how would you prove it works?

Space for answers:

<!-- Write your own answers here, in your own words. -->

---

## 8. Make it yours

- [ ] Rewrite the `lessons-learned.md` entries in your own voice, from what you personally observed
- [ ] Add **one feature yourself** (a tool or a scenario) without the other agent, and verify it with the evals
- [ ] Pick one design decision you disagree with, change it, and record the outcome

---

## Suggested time split

- ~1/3 reading code and transcripts
- ~1/3 break-it experiments with measurements
- ~1/3 writing up and practicing explanations out loud

---

## Notes

<!-- Running notes, surprises, open questions. -->
