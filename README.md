# Follow-up engine (take-home)

MVP of an automated follow-up engine: ingests quote/event data, ranks open
quotes by follow-up urgency with a reason, drafts a message, and requires a
human approval before a (simulated) send. Python stdlib only — no
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

`--now` picks the point in time to evaluate against (seed data is dated Aug
2026); omit it to use the real current time. Tests: `python3 -m unittest
discover -s tests -v`. Delete `followup.db` to start over.

## The follow-up policy and why

Signals (viewed / replied / messaged) come from the **event log**, sorted by
timestamp — not file order, and not `quotes.json`'s own `last_contact_at`
field, which disagrees with the event stream for several seed quotes (e.g.
`Q-1002` has later events than its `last_contact_at`; `Q-1005`'s is `null`
despite having a `message_sent` event). `quotes.status`, by contrast, is
treated as authoritative — it's a clean field with nothing in the data
contradicting it.

Each open quote gets at most one reason, the highest-priority rule that fires:

1. **`reply_pending`** — customer replied, we haven't responded. Most
   urgent: they're engaged, and silence is the fastest way to lose a warm
   deal. (2h grace period so a reply 5 minutes old isn't paged immediately.)
2. **`viewed_no_response`** — viewed in the last 72h, no reply, no nudge
   sent since. They're actively looking — strike while it's hot.
3. **`stale_big`** — $5,000+, no contact in 4+ days. Bigger deals get
   chased sooner; losing one costs more.
4. **`stale_general`** — any amount, no contact in 7+ days. Catch-all so
   small quotes don't fall through the cracks, just on a longer fuse.

A `quote_viewed` event drives rule 2 but deliberately does **not** reset the
"last contact" clock used by rules 3/4 — a passive view isn't a conversation
touchpoint, and letting it indefinitely suppress "say something" felt wrong.
Thresholds live as named constants in `policy.py`, picked by judgment (not
given by the spec) — they'd really come from the product owner's sense of
"too pushy" vs. "too slow."

**Other signals I'd want but don't have data for:** a competing open quote
elsewhere, the tech's relationship with that customer, time-of-day/day-of-
week, and outcome feedback (did past follow-ups actually convert) to let
thresholds self-tune.

## Guardrails

- **Never message an accepted/dismissed quote** — checked when generating
  candidates, re-checked live at send time (including against
  `quote_accepted` *events*, since `quotes.json`'s `status` is a batch
  snapshot that can lag a more real-time event stream), and enforced as
  hard DB triggers so it's impossible even if a future code path bypasses
  the Python checks.
- **Cooldown, scoped per customer phone, not per quote** — the seed data
  has two open quotes for the same customer (`Q-1003`/`Q-1025`, Karen
  Nguyen); a message to one puts both on cooldown. This is checked at
  draft time *and* atomically re-checked at send time inside one
  transaction — the atomic send-time check is what actually matters:
  during review I found and fixed a real race where two genuinely
  concurrent `send()` calls could both believe "no recent contact" and
  both go out. Verified closed with real concurrent threads, repeated
  trials, no artificial synchronization.
- **No double-send on retry or re-run** — sending is `INSERT OR IGNORE`
  keyed by `message_id`, so retrying a send (or the whole engine) can
  never produce a second outbound message. Drafting is similarly
  idempotent, backed by a DB unique index, not just app logic.
- **Approve before send is enforced by state, not convention** — `send()`
  raises if the message isn't `approved` (or a previously `failed` retry);
  there's no path that sends a bare draft.

## Known limitations

- Phone normalization (`phone.py`) is a simple US-centric heuristic, not a
  full i18n library — fine for this seed data, not for a real multi-region
  deployment.
- SQLite is single-writer; WAL mode means reads don't block behind writes,
  but this isn't a real multi-writer datastore at scale (see below).
- No real SMS/email provider — the `outbox` table is the stand-in, designed
  as the seam where one would plug in.
- No audit trail for drafts a human edits or explicitly rejects (they just
  stay in `draft`).

## Scaling to a parent company with 50 shops

The biggest change isn't technical capacity, it's that "customer," "quote,"
and "follow up" stop being company-wide concepts and become per-shop
configuration. Phone numbers and event streams would need a `shop_id` (or
`tenant_id`) on every row, including in the event stream itself, and every
query in `policy.py` would need to scope by it — not just for correctness,
but because a shop's customer list, its outbound phone number, and its
message templates have to stay isolated from the other 49. More
interestingly, "what follow up means" is explicitly called out as varying by
shop: one owner wants aggressive same-day nudges, another thinks a text
within 48h is pushy and prefers a 5-day cadence, and a third doesn't do SMS
at all. That argues for pulling the current hardcoded constants in
`policy.py` (cooldown, stale-day thresholds, the big-quote dollar cutoff,
even which rules are enabled) into a per-shop policy config, with sane
company-wide defaults a shop can override — while keeping the guardrails
(never message accepted/dismissed, cooldown, no double-send) as
non-negotiable platform-level invariants that no shop config can weaken,
since those are safety properties, not preferences.

The second-order change is operational: with 50 shops producing events
concurrently, SQLite's single-writer model stops being adequate, and the
"approve" step stops being one owner eyeballing a terminal — it needs to be
a real multi-user surface (who approved what, per-shop visibility, an audit
trail of drafts that were edited or rejected, not just sent). I'd also want
per-shop rate limits and a kill switch (pause all sends for shop X) as
first-class operations, since a bug in one shop's policy config shouldn't be
able to spam that shop's customers while the other 49 keep running
normally, and support needs a fast way to stop it without redeploying code.

## What I'd build next, and where I stopped

- A real integration behind `send()`: provider delivery status via webhook,
  retry-with-backoff instead of a permanent `failed`.
- Let a human edit a draft's body before approving, not just approve/reject
  it verbatim.
- A `rejected` terminal state for a draft a human declines, distinct from
  "not approved yet."
- Per-shop policy config as described above, plus a backtest harness against
  historical outcomes.
- A small local web view instead of the CLI — not a logic change, just
  better for an owner reviewing 20+ drafts a morning.
- Fuzz tests on the ingester beyond the hand-written cases.

The core build (ingest/policy/drafting/draft-approve-send/CLI/tests/README)
was done to the stated ~3-hour scope. I then went back through several
rounds of adversarial self-review — including a structured review
specifically trying to break the guardrails with real scripts against real
data — which found and fixed real bugs, most notably the concurrency race
above. That's additional work beyond the original time box, done because it
was asked for, not because the first pass was sloppy; I'm noting it since
the assignment is explicit that the time budget matters. Everything above
is genuine next-iteration scope, not a loose end in what's here.

## Notes on AI use

Built with Claude Code; the exported conversation (`transcript.txt`) is
included alongside this README. The judgment calls above — rule tiering,
what counts as "contact," the per-customer cooldown scope, the guardrail
design — are mine; the agent turned that design into code and, at my
direction, ran real adversarial testing (including forced concurrency races
against real seed data) to verify it rather than just asserting it works.
