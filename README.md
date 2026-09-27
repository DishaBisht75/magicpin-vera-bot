# magicpin AI Challenge — Submission

## Approach

Deterministic, rules/template-based composer — **no LLM call in the hot path**.

The rubric's #1 named anti-pattern is hallucinated facts, and the #1 required
property is determinism. A composer that can *only* reference fields already
present in the pushed `category` / `merchant` / `trigger` / `customer`
contexts structurally cannot hallucinate, and is trivially deterministic
(same input → same output, no temperature to fix).

`composer.py` works in three steps:
1. **`pick_framing(trigger)`** — routes the trigger to one of ~18 "framing
   families" (digest, compliance, perf_dip, perf_spike, recall, milestone,
   competitor, seasonal opportunity, planning-intent, …) using keyword
   matching on `kind`/`scope`/`source` — not a hardcoded switch on the 24
   seed `kind` values. This matters because the judge injects *new* trigger
   kinds mid-test (testing-brief §4, Phase 3); a keyword router still places
   an unseen kind into a sensible family instead of falling through.
2. **`pick_anchor(...)`** — pulls the one verifiable fact the message will
   hang on (a digest item looked up by id, a performance delta, a recall
   due-date + slots, a competitor's name/distance/offer, …). When a trigger
   is "thin" (many generated triggers only carry `{"placeholder": true,
   "metric_or_topic": "<kind>"}`), it falls back to a humanized version of
   the kind name rather than dumping raw JSON.
3. **A per-framing builder** turns the anchor + merchant/category context
   into the actual WhatsApp body, respecting category voice (taboo-word
   scrub), Hindi-English code-mix when the merchant/customer's language
   preference calls for it, and a single CTA.

`reply_engine.py` handles `/v1/reply`:
- **Auto-reply detection**: keyword heuristics (canned phrases like "thank
  you for contacting…") plus a verbatim-repeat fallback (3+ identical
  incoming messages ⇒ auto-reply), per the brief's own hint. First hit gets
  one gentle re-ask; second hit exits gracefully — never burns more than
  one extra turn on a bot.
- **Intent handoff**: explicit "yes / let's do it / haan chaliye" switches
  straight to action mode instead of re-qualifying. This is "Pattern D" in
  the brief — the one named anti-pattern for conversation flow — and is
  also the *entire* `active_planning_intent` trigger kind in the dataset,
  so it's handled at both the tick-composition layer and the reply layer.
- **Not-interested → end**, **hostile → de-escalate and offer an out**,
  **off-topic → polite redirect back to mission**.
- **Anti-repetition**: every sent body is tracked per-conversation; a
  generic acknowledgment reply varies its wording rather than repeating
  itself verbatim.

`bot.py` is thin wiring: in-memory context store keyed by `(scope,
context_id)` with idempotent version handling, a suppression-key set so the
same trigger never fires twice, and per-conversation state for the reply
engine.

## Tradeoffs

- **No LLM = less prose variety.** Real Vera conversations read more
  fluidly; this composer's Hindi-English mixing is a fixed set of
  connective phrases rather than a genuinely fluent bilingual voice. A
  stronger version would use an LLM as a *post-processing polish pass*
  (rewrite this grounded, fact-checked draft in natural Hinglish) while
  keeping the fact-selection and CTA logic deterministic — I didn't build
  that layer given the time available, but the architecture (anchor →
  builder → body) makes it a clean place to insert one.
- **Framing coverage is broad but not exhaustive.** ~18 families cover
  every kind in the seed dataset plus a generic fallback for anything
  unseen. A wrong-family match on a truly novel injected trigger would
  produce a generically-worded-but-still-grounded message rather than a
  perfectly-tailored one.
- **Multi-turn depth is shallow.** `reply_engine.py` classifies single
  turns well but doesn't track a broader dialogue plan (e.g., deciding
  *which* of several open threads to advance next). Fine for the 3-5 turn
  replay scenarios described, weaker for longer real conversations.

## What additional context would have helped most

- A confidence/priority score on `available_triggers` in `/v1/tick` — right
  now the bot has to guess trigger importance from `urgency` alone when
  deciding what to send in a tick with several candidates for one merchant.
- Real example transcripts of the *customer-facing* flows (only 2 are given
  in the brief; the merchant-facing side has far more reference material).

## Files

- `composer.py` — pure functions, no I/O. Unit-testable via `test_local.py`.
- `reply_engine.py` — conversation state + reply classification.
- `bot.py` — FastAPI app exposing the 5 required endpoints.
- `test_local.py` — runs `compose()` against the 30 canonical test pairs
  without needing a running server; writes `submission.jsonl`.
- `requirements.txt`

## Running it

```bash
pip install -r requirements.txt
uvicorn bot:app --host 0.0.0.0 --port 8080
```

In another terminal, from the challenge zip root (with `expanded/` already
generated via `dataset/generate_dataset.py`):

```bash
export BOT_URL=http://localhost:8080
python judge_simulator.py
```

Or sanity-check the composer directly without a server:

```bash
cp composer.py /path/to/challenge-zip/
cd /path/to/challenge-zip
python3 test_local.py --dataset expanded
```

## Deploying

Any host that gives a public HTTPS URL works (Render, Railway, Fly.io,
a VPS with a reverse proxy). No external dependencies beyond FastAPI —
no LLM API key required, since composition is fully rule-based. Set
`TEAM_NAME` / `TEAM_MEMBERS` / `CONTACT_EMAIL` at the top of `bot.py`
before deploying.
