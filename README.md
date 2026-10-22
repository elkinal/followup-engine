# Follow-up engine (take-home)

MVP of an automated follow-up engine. Ingests quote/event data, ranks open
quotes by follow-up urgency with a reason, drafts a message, and requires
human approval before a (simulated) send. Python stdlib only, no
dependencies to install.

## How to run it

Requires Python 3.11+. Run every command from this project's root (the
folder containing this README and `followup_engine/`, `seed/`, `tests/`).

```bash
python3 -m followup_engine.cli ingest                              # load seed data
python3 -m followup_engine.cli candidates --now 2026-08-16T00:00:00Z  # prioritized list (read-only)
python3 -m followup_engine.cli draft --now 2026-08-16T00:00:00Z       # create drafts
python3 -m followup_engine.cli drafts --status draft                  # review them
python3 -m followup_engine.cli approve MESSAGE_ID                     # human approves
python3 -m followup_engine.cli send MESSAGE_ID                        # then sends
python3 -m followup_engine.cli outbox                                 # see what went out
```

`--now` sets the point in time to evaluate against (seed data is dated Aug
2026). Omit it to use the real current time. Tests: `python3 -m unittest
discover -s tests -v`. Delete `followup.db` to start over.

## The follow-up policy and why

Signals (viewed, replied, messaged) come from the event log, sorted by
timestamp, not file order. Not from `quotes.json`'s `last_contact_at`
field either: it disagrees with the event stream for several seed quotes
(`Q-1002` has later events than its `last_contact_at`; `Q-1005`'s is `null`
despite having a `message_sent` event). `quotes.status` is trusted, since
it's a clean field and nothing in the data contradicts it.

Each open quote gets at most one reason, the highest-priority rule that fires:

1. **`reply_pending`.** Customer replied, we haven't responded. Most
   urgent: they're engaged, and silence is the fastest way to lose a warm
   deal. 2 hour grace period so a 5 minute old reply isn't paged instantly.
2. **`viewed_no_response`.** Viewed in the last 72h, no reply, no nudge
   sent since. They're actively looking.
3. **`stale_big`.** $5,000+, no contact in 4+ days. Bigger deals get
   chased sooner since losing one costs more.
4. **`stale_general`.** Any amount, no contact in 7+ days. Catches small
   quotes too, just on a longer fuse.

A `quote_viewed` event drives rule 2 but deliberately does not reset the
"last contact" clock used by rules 3 and 4. A passive view isn't a
conversation touchpoint, and it shouldn't indefinitely suppress "say
something." Thresholds live as named constants in `policy.py`, picked by
judgment, not given by the spec.

**Other signals I'd want but don't have data for:** a competing open quote
elsewhere, the tech's relationship with that customer, time of day, and
outcome feedback (did past follow-ups actually convert) to let thresholds
self-tune.

## Guardrails

- **Never message an accepted or dismissed quote.** Checked when generating
  candidates, re-checked live at send time (including `quote_accepted`
  events, since `quotes.json`'s `status` is a batch snapshot that can lag
  the event stream), and enforced as hard DB triggers, so it holds even if
  a future code path skips the Python checks.
- **Cooldown, scoped per customer phone, not per quote.** The seed data has
  two open quotes for the same customer (`Q-1003`/`Q-1025`, Karen Nguyen).
  A message to one puts both on cooldown. Checked at draft time and
  re-checked atomically at send time. During review I found a real race
  where two genuinely concurrent `send()` calls could both read "no recent
  contact" and both go out. Fixed, and verified closed with real
  concurrent threads across repeated trials.
- **No double-send on retry or re-run.** Sending is `INSERT OR IGNORE`
  keyed by `message_id`, so retrying can never produce a second message.
  Drafting is similarly idempotent, backed by a DB unique index.
- **Approve before send, enforced by state.** `send()` raises if the
  message isn't `approved`. There's no path that sends a bare draft.

## Known limitations

- Phone normalization (`phone.py`) is a simple US-centric heuristic, not a
  full i18n library.
- SQLite is single-writer. Fine for one shop, not for 50.
- No real SMS/email provider. The `outbox` table is the stand-in.
- No audit trail for a draft a human edits or explicitly rejects.

## Scaling to a parent company with 50 shops

Data and policy need to become per-shop, not shared. Every quote, event,
and customer needs a `shop_id`, and every query needs to filter by it.
Otherwise one shop's customers and phone numbers leak into another's
results. Shops also disagree on what "follow up" means. One wants same-day
texts, another waits 5 days and calls that polite, a third skips SMS
entirely. So cooldown length, stale-day windows, and the "big quote"
cutoff should become per-shop settings, not one fixed value for everyone.
The three safety guardrails stay fixed for every shop though. They protect
customers, not preferences, so no shop config can turn them off.

More shops also means more moving parts operationally. SQLite can't handle
50 shops writing at once, so this needs a real multi-writer database.
Approving messages can't stay one person clicking through a terminal
either. It needs a screen that shows who approved what, scoped to their
shop. And support needs a kill switch to pause one shop's sends instantly,
without touching the other 49.

## What I'd build next, and where I stopped

- A real integration behind `send()`: provider delivery status via webhook,
  retry with backoff instead of a permanent `failed`.
- Let a human edit a draft's body before approving, not just approve or
  reject it verbatim.
- A `rejected` terminal state for a draft a human declines, distinct from
  "not approved yet."
- Per-shop policy config as described above, plus a backtest harness
  against historical outcomes.
- A small local web view instead of the CLI, not a logic change, just
  better for an owner reviewing 20+ drafts a morning.
- Fuzz tests on the ingester beyond the hand-written cases.

The core build (ingest, policy, drafting, draft/approve/send, CLI, tests,
README) was done to the stated 3-hour scope. I then went through several
rounds of adversarial self-review, including a structured pass aimed
specifically at breaking the guardrails with real scripts against real
data, which found and fixed real bugs, most notably the concurrency race
above. That's additional work beyond the original time box, done because it
was asked for. Noting it since the assignment is explicit that the time
budget matters. Everything above is genuine next-iteration scope, not a
loose end in what's here.

## Notes on AI use

Built with Claude Code. The exported conversation (`transcript.txt`) is
included alongside this README. The judgment calls above, rule tiering,
what counts as "contact," the per-customer cooldown scope, the guardrail
design, are mine. The agent turned that design into code and, at my
direction, ran real adversarial testing, including forced concurrency races
against real seed data, to verify it rather than just asserting it works.
