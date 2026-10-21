# Follow-up engine (take-home)

Automated follow-up engine MVP: ingests quote/event data, ranks open quotes
by follow-up urgency with a documented reason, drafts a message, and
requires a human approve step before a (simulated) send. Python stdlib only
-- no dependencies to install.

## How to run it

Requires Python 3.11+ (uses `datetime.fromisoformat` with `Z` suffixes).

```bash
cd followup_engine

# 1. Load the seed data into a local SQLite db (followup.db)
python3 -m followup_engine.cli ingest

# 2. See the prioritized, reasoned candidate list (read-only)
#    --now lets you pick a point in time to evaluate against; the seed data
#    lives in Aug 2026, so pass a timestamp in that range to see interesting
#    output. Omit --now in a real deployment to use the actual current time.
python3 -m followup_engine.cli candidates --now 2026-08-16T00:00:00Z

# 3. Turn today's top candidates into drafts (idempotent -- safe to re-run)
python3 -m followup_engine.cli draft --now 2026-08-16T00:00:00Z

# 4. Review drafts
python3 -m followup_engine.cli drafts --status draft

# 5. Human approves, then sends (two separate, deliberate steps)
python3 -m followup_engine.cli approve <message_id>
python3 -m followup_engine.cli send <message_id>

# 6. See what actually went out
python3 -m followup_engine.cli outbox
```

Run the tests with:

```bash
python3 -m unittest discover -s tests -v
```

`followup.db` is created next to this README on first `ingest` and is safe
to delete (`rm followup.db`) to start over.

## The follow-up policy and why

For every `open` quote, signals are derived from the **event log**, not from
`quotes.json`'s `last_contact_at` field -- I found that field disagrees with
the event stream in the seed data (e.g. `Q-1002` has a `customer_replied`
and `message_sent` event *after* its `last_contact_at`; `Q-1005` has a
`message_sent` event but `last_contact_at: null`). Treating it as a stale
cache from some other system of record, rather than ground truth, seemed
like the safer read of "messy input."  `quotes.status`, on the other hand,
*is* treated as authoritative for guardrail purposes (never follow up on an
`accepted`/`dismissed` quote), since it's a clean snapshot field and nothing
in the data contradicts it.

Events are always sorted by timestamp (not file order or insertion order)
before computing signals, since the event stream is explicitly out-of-order,
and duplicate `event_id`s are deduplicated at ingest.

Each open quote is evaluated against four rules, checked in priority order
(a quote gets at most one reason -- the highest-priority one that applies):

1. **`reply_pending`** (highest) -- the customer replied and we haven't
   messaged them since. This is the most urgent case: they engaged, and
   leaving them hanging is the clearest way to lose a deal that was already
   warm. (2-hour grace period so a reply 5 minutes old doesn't immediately
   page someone.)
2. **`viewed_no_response`** -- the customer viewed the quote in the last 72h,
   hasn't replied, and we haven't already nudged them since that view. This
   is the "strike while the iron's hot" case -- they're actively looking.
3. **`stale_big`** -- no contact (outbound message *or* reply; a view alone
   doesn't count, see below) in 4+ days on a quote worth $5,000+. Bigger
   deals get chased sooner because the cost of losing one is higher.
4. **`stale_general`** -- no contact in 7+ days, regardless of size. A
   catch-all so small quotes don't fall through the cracks indefinitely,
   just on a longer fuse.

A brand-new quote with no activity yet is correctly *not* a candidate --
there's nothing to follow up on.

Notable judgment call: a `quote_viewed` event is a strong buying-interest
signal and drives rule 2, but it does **not** reset the "last contact" clock
used by rules 3/4. The customer looking at a quote isn't a conversation
touchpoint from us; I didn't want a passive view to quietly suppress "it's
been N days, say something" forever.

Guardrails (enforced in `service.py`, the only module that writes to
`messages`/`outbox` -- plus, for the first one, in the schema itself):

- **Never message an `accepted`/`dismissed` quote** -- checked when
  generating candidates (`policy.py` only looks at `status = 'open'`), again
  in Python at send time in case it changed between draft and approval, and
  as a hard constraint in `db.py`: three triggers (`block_draft_for_non_open_quote`,
  `block_approve_for_non_open_quote`, `block_send_for_non_open_quote`) make
  it impossible to insert a draft, approve one, or write an `outbox` row for
  a quote that isn't open, independent of what any Python code path checks.
  I added these after first building the guardrail only in `service.py` --
  that's correct for every path that goes through it today, but a schema
  constraint is the only thing that stays true even if a future code path
  (or a bug) writes to these tables directly. `service.py` catches the
  resulting `IntegrityError` and turns it into the same clean
  `GuardrailViolation` a caller would get from the Python-level check, so
  nothing leaks a raw SQLite error (tested in `test_db_constraints.py` and
  `test_guardrails.py`).
- **Cooldown, by customer phone number, not by quote** -- the seed data has
  two separate open quotes for the same customer (`Q-1003`/`Q-1025`, Karen
  Nguyen, same phone). The guardrail's intent is "don't spam this human,"
  so a message to one of her quotes puts both on cooldown (48h) for the rest
  of the batch, even though they're different `quote_id`s.
- **No double-send on retry/re-run** -- `send()` is keyed by `message_id`
  and writes to the `outbox` table via `INSERT OR IGNORE` on its primary
  key, so retrying an identical send call (crash, timeout, re-running the
  whole engine) can never produce a second outbound message. Draft
  generation is similarly idempotent: a quote with an already-open
  draft/approved message is skipped, backed by a partial unique index
  (`uniq_open_message_per_quote`) as a database-level backstop, not just
  application logic.
- **Approve before send is enforced by state, not convention** -- `send()`
  raises if the message isn't `approved` (or a previously `failed` retry).
  There's no path that sends a bare `draft`.

Tunable thresholds (grace periods, windows, the "big quote" cutoff, cooldown
length) live as named constants at the top of `policy.py` rather than being
hardcoded inline, so they're easy to find and justify, but I picked the
actual numbers by judgment, not from any given spec -- they'd really come
from whoever owns this product talking to techs about what "too pushy" and
"too slow" feel like in practice.

**Other signals I'd want but don't have data for:** whether the customer has
an open *competing* quote elsewhere (if discoverable), the tech's own
preference/relationship with that customer, time-of-day/day-of-week (don't
text at 11pm), and outcome feedback (did past follow-ups on similar quotes
actually convert, to let the thresholds self-tune over time).

### Gaps I found and either fixed or consciously left

Auditing my own guardrails after the first pass turned up a few real bugs,
which I fixed and backed with tests (`test_db_constraints.py`,
`test_ingest.py`, `test_drafting.py`, `test_timeutil.py`):

- **Ingest crashed, and lost already-processed rows, on a malformed value**
  (e.g. a present-but-`null` `amount`, an unparseable `created_at`) instead
  of skipping that one row -- `ingest.py` now validates field *values*, not
  just key presence, and wraps each row's insert so one bad record can never
  take the rest of the batch down with it.
- **`parse_ts(None)` raised `AttributeError`** instead of a catchable
  `TypeError`/`ValueError`, which meant a literal JSON `null` timestamp
  slipped past the fix above. Fixed in `timeutil.py`.
- **`draft_message()` crashed on a blank/whitespace-only `customer_name`**
  (`"".split()[0]` -> `IndexError`). Now falls back to a generic greeting.
- **The "never message a non-open quote" guardrail was Python-only.** Added
  as schema-level triggers (see above) so it's structurally impossible, not
  just checked.

Found but **not** fixed, since they're either out of scope for an MVP or
need a product decision I can't make unilaterally:

- `customer_phone` isn't normalized, so the same person with two differently
  formatted phone numbers would bypass the cross-quote cooldown guardrail.
- Our outbound `send()` always defaults to SMS; it doesn't look at which
  channel (`sms`/`email`) the customer has actually used before.
- No record of *who* approved a message -- relevant for an audit trail once
  this is multi-user, not just a single owner.
- SQLite is single-writer; concurrent CLI invocations can hit "database is
  locked" with no retry. Fine for one shop's local MVP, not for the 50-shop
  scale discussed below.

## Scaling to a parent company with 50 shops

The biggest change isn't technical capacity, it's that "customer," "quote,"
and "follow up" stop being company-wide concepts and become per-shop
configuration. Phone numbers and event streams would need a `shop_id` (or
`tenant_id`) on every row, including in the event stream itself, and every
query in `policy.py` would need to scope by it -- not just for correctness,
but because a shop's customer list, its outbound phone number, and its
message templates have to stay isolated from the other 49. More
interestingly, "what follow up means" is explicitly called out as varying by
shop: one owner wants aggressive same-day nudges, another thinks a text
within 48h is pushy and prefers a 5-day cadence, and a third doesn't do SMS
at all. That argues for pulling the current hardcoded constants in
`policy.py` (cooldown, stale-day thresholds, the big-quote dollar cutoff,
even which rules are enabled) into a per-shop policy config, with sane
company-wide defaults a shop can override -- while keeping the guardrails
(never message accepted/dismissed, cooldown, no double-send) as
non-negotiable platform-level invariants that no shop config can weaken,
since those are safety properties, not preferences.

The second-order change is operational: with 50 shops producing events
concurrently, SQLite's single-writer model stops being adequate, and the
"approve" step stops being one owner eyeballing a terminal -- it needs to be
a real multi-user surface (who approved what, per-shop visibility, an audit
trail of drafts that were edited or rejected, not just sent). I'd also want
per-shop rate limits and a kill switch (pause all sends for shop X) as
first-class operations, since a bug in one shop's policy config shouldn't be
able to spam that shop's customers while the other 49 keep running
normally, and support needs a fast way to stop it without redeploying code.

## What I'd build next with another day

- A real integration behind `send()`: the current outbox table is a stand-in
  for a provider call (Twilio for SMS, something transactional for email).
  In production I'd add a `provider_message_id` column, handle provider-side
  async delivery status (`queued` -> `delivered`/`failed`) via webhook, and
  retry failed sends with backoff rather than leaving them `failed`
  permanently.
- Let a human edit a draft's body before approving, not just approve/reject
  it verbatim -- right now the template text is final once generated.
- A `rejected`/`skipped` terminal state for drafts a human looks at and
  decides *not* to send (right now they just sit in `draft` forever),
  distinct from "not approved yet."
- Per-shop policy config as described above, plus a small backtest harness:
  replay the policy against historical events and (if I had outcome data)
  check whether its ranking actually correlates with quotes that converted.
- Swap the CLI for a small local web view -- not because the policy/guardrail
  logic would change, but an owner reviewing 20+ drafts a morning wants a
  list with inline approve buttons, not a terminal one id at a time.
- Property-based/fuzz tests on the event ingester (random orderings,
  duplicate injection, missing fields) beyond the hand-written cases already
  in `tests/test_ingest.py`.

## Notes on AI use

Built with Claude Code. The full conversation transcript is included per the
assignment instructions (see the exported conversation alongside this
README). I made the judgment calls above myself -- the tiering of the four
rules, what counts as "contact" vs. a passive signal, the phone-based
cooldown scope, and the idempotency design -- and used the agent mainly to
turn that design into code quickly, then verified behavior end-to-end
against the seed data (see the manual guardrail walkthroughs in the
transcript, e.g. simulating a quote being accepted between approval and
send) in addition to the automated test suite.
