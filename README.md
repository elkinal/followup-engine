# Follow-up engine (take-home)

Automated follow-up engine MVP: ingests quote/event data, ranks open quotes
by follow-up urgency with a documented reason, drafts a message, and
requires a human approve step before a (simulated) send. Python stdlib only
-- no dependencies to install.

## How to run it

Requires Python 3.11+ (uses `datetime.fromisoformat` with `Z` suffixes).

Run every command below from this project's root directory -- the one
directly containing this README and the `followup_engine/`, `seed/`, and
`tests/` folders (however your checkout happens to be named; don't `cd`
into the `followup_engine/` folder itself, since that's the Python package,
one level too deep for `python3 -m` to find it):

```bash
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

# 5. Human approves, then sends (two separate, deliberate steps).
#    Copy a real id from step 4's output in place of MESSAGE_ID below --
#    approve defaults --by to your OS username; send defaults --channel to
#    whichever channel the draft was written for (see "follow-up policy").
python3 -m followup_engine.cli approve MESSAGE_ID
python3 -m followup_engine.cli send MESSAGE_ID

# 6. See what actually went out
python3 -m followup_engine.cli outbox
```

Run the tests with:

```bash
python3 -m unittest discover -s tests -v
```

`followup.db` is created next to this README on first `ingest` and is safe
to delete (`rm followup.db`) to start over. Schema changes in this project
only apply via `CREATE TABLE IF NOT EXISTS`, so an existing `followup.db`
from before a schema change (e.g. the `channel`/`approved_by` columns added
in the audit section below) won't pick it up automatically -- delete it and
re-ingest. A real deployment would need actual migrations; out of scope
here.

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
  so a message to one of her quotes puts both on cooldown (48h), even though
  they're different `quote_id`s. This used to only hold *within one
  `create_drafts()` call* -- found by literally running the CLI against the
  full seed data twice, five minutes apart: the first run drafted a message
  for one of Karen's quotes and correctly skipped the other for cooldown,
  but the second run (simulating a re-run before the first batch was
  approved) drafted the *other* quote too, because cooldown was computed
  from actual sends plus same-batch drafts only -- never from a different
  run's still-open draft. Approving and sending both then produced two real
  outbound messages to the same customer a minute apart, which I confirmed
  by actually doing it end-to-end through the CLI before fixing it. Two
  fixes, mirroring how the accepted/dismissed guardrail already has both a
  draft-time check and a send-time re-check: `create_drafts()` now also
  treats any existing open (unsent) draft/approved message as provisional
  contact, so the conflicting draft is never created; and `send()`
  independently re-checks cooldown immediately before writing to the
  outbox, since it's the function with the actual side effect and can't
  assume draft-time prevention ever ran (tested directly by inserting two
  open messages for the same phone and proving `send()` refuses the second,
  bypassing `create_drafts()`'s own prevention entirely in
  `TestCooldownIsAuthoritativeAtSendTime`).
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

A second audit pass (prompted by explicitly going looking for more) found a
subtler correctness bug plus more CLI robustness gaps:

- **Timestamps were stored un-normalized, and every ordering/filter in
  `policy.py` is a raw SQL string comparison** (`WHERE timestamp <= ?`,
  `ORDER BY timestamp`). A validly-parseable timestamp in a non-`Z` form
  (e.g. a `+08:00` offset) was stored as-is; its string sort order doesn't
  match its real chronological position, so an event could silently vanish
  from signal computation even though it parsed correctly. My first-pass fix
  (accept anything `parse_ts` can parse) actually made this worse by letting
  non-canonical formats into storage. Fixed by normalizing every timestamp
  to the canonical `Z`-suffixed UTC string at ingest time, before it's
  written -- confirmed with a repro where an event at the real boundary of
  "now" was being dropped, then re-verified it's found after the fix.
- **An `event_id` reused for genuinely different event content was silently
  absorbed into the "duplicate" count.** The dedup logic was only designed
  for byte-identical webhook redelivery; a collision between two *different*
  events sharing an id (which should never happen, but nothing checked)
  would make the second one disappear with no trace. Now compared against
  the stored content: true duplicates still count as duplicates, but a
  content mismatch is surfaced in `events_skipped` instead.
- **Several CLI failure paths leaked raw Python tracebacks**: a missing
  `quotes.json`/`events.jsonl`, a `--db` path whose parent directory doesn't
  exist, and a `quotes.json` whose top-level JSON isn't a list. `ingest.py`
  now raises a dedicated `IngestError` for input-shape problems (distinct
  from a single bad row, which is still skipped and logged), and `cli.py`
  catches that plus `sqlite3.OperationalError` alongside the existing
  guardrail/value errors.
- Backfilled test coverage that didn't exist for `cli.py` at all
  (`test_cli.py`: clean-error-message tests plus a full ingest -> draft ->
  approve -> send -> outbox run through the actual CLI entry point).

A final completeness pass (re-checking repo hygiene and re-reading every
file fresh rather than trusting my own summary of it) found two more:

- **`.gitignore` didn't cover the `followup.db-wal`/`followup.db-shm`
  sidecar files WAL mode creates.** They're normally cleaned up on a clean
  connection close, but confirmed they exist while a connection is open and
  would be left behind (and un-ignored) if a process were ever killed
  mid-operation. Added to `.gitignore`.
- **The same "silently reused identity" bug I'd already fixed for
  `event_id` also existed for quote `id`s, unfixed.** Two quotes sharing an
  `id` within one `quotes.json` file silently resolved last-write-wins via
  the upsert, reported as "2 upserted" with zero indication one had just
  clobbered the other. I'd noticed and mentioned this verbally earlier in
  the process but never actually fixed or written it down -- caught on a
  final re-check that specifically looked for exactly that kind of gap.
  Fixed the same way as the `event_id` case: still applies last-write-wins
  (that's a reasonable default), but now flags it as a warning instead of
  staying silent. A normal re-ingest that legitimately updates a quote's
  status across *separate* runs is unaffected -- only a collision *within
  the same file* triggers it.

A third pass then implemented the four items from the previous pass that
were "found but not fixed" above:

- **Phone numbers are now normalized at ingest** (`phone.py`:
  `normalize_phone`) to an E.164-ish canonical form before being stored, so
  `"+19175552003"` and `"(917) 555-2003"` for the same customer resolve to
  one identity and the cross-quote cooldown guardrail can't be bypassed by a
  formatting difference. This is a simple regex-based, US-centric
  normalizer, not a full i18n phone library -- a real deployment serving
  non-US shops should replace it with one (e.g. `libphonenumber`); I called
  this out explicitly rather than let a hand-rolled heuristic look more
  authoritative than it is.
- **Outbound channel is now chosen per-customer, not hardcoded to SMS.**
  `policy.py`'s `QuoteSignals.preferred_channel` looks at the customer's own
  event history -- their most recent reply's channel wins (they chose it
  themselves), falling back to whichever channel we last messaged them on,
  falling back to SMS only if there's no history at all. The chosen channel
  is stored on the draft (`messages.channel`, new column) at draft time and
  used by `send()` unless explicitly overridden. Verified against the real
  seed data: `Q-1007` drafts on `email` because that customer's most recent
  event is a `customer_replied` with `channel: "email"`.
- **Every approval now records who approved it** (`messages.approved_by`,
  new column, required by `service.approve()` and enforced by the same
  CHECK constraint pattern as `approved_at`). The CLI defaults it to the
  current OS user (`getpass.getuser()`) so a single-owner setup needs no
  extra flags, with `--by` to override for a shared/multi-user setup.
- **Reduced (not eliminated) the SQLite concurrency gap.** `db.connect()`
  now requests WAL journal mode, which is the actually meaningful change:
  unlike the default rollback-journal mode, a writer never blocks a reader
  under WAL, so `candidates`/`drafts`/`outbox` can run while a `draft`/
  `send` is in progress. I also added an explicit `PRAGMA busy_timeout =
  5000`, but want to be honest that this one doesn't change behavior --
  Python's `sqlite3` module already defaults new connections to a 5s busy
  timeout, confirmed by checking `PRAGMA busy_timeout` on a connection
  that never set it. I kept the line anyway, as explicit, change-controlled
  documentation of a value the code depends on, not as a fix for a bug that
  turned out not to exist. Verified the retry-not-immediate-failure
  behavior directly: with one connection holding an open write transaction,
  a second connection's write blocks and then succeeds once the first
  commits, rather than raising "database is locked" right away. None of
  this replaces a real multi-writer datastore at the 50-shop scale
  discussed below -- it's a mitigation appropriate for a single local
  SQLite file.

### A final correctness-and-coverage verification pass

I went back and actually measured branch coverage with `coverage.py`
(installed in a throwaway venv -- not a project dependency) rather than
estimating it, then closed the gaps that were real risk, not just missing
percentage: 81% -> 95% overall branch coverage, adding tests for the CLI's
`candidates`/`drafts` commands (previously never invoked by any test despite
being 2 of the 7 commands), several `ingest.py` validation branches that
were exercised manually at some point in this process but never promoted to
a permanent test (notably the exact `parse_ts(None)` bug from the second
pass), and the `approve()`-time trigger conversion. Two small real fixes
came out of that: a dead function (`timeutil.utcnow_str`, defined, never
called) was deleted instead of tested, and `tech_name`/`id` were missing the
same value-validation every other required quote field already had
(`id` didn't `.strip()` the way `customer_name`/`tech_name` did, so a
whitespace-only id would have been silently accepted).

The actual catch, though, came from exhaustively re-running the full CLI
against the real seed data rather than trusting the existing test suite:
running `draft` twice, five minutes apart, produced two open drafts for
Karen Nguyen (`Q-1003`/`Q-1025`) instead of one -- documented in detail
above under "Cooldown, by customer phone number." That's a real,
confirmed-exploitable violation of one of the three guardrails the
assignment names explicitly, and it existed because the test suite had
never exercised "call `create_drafts()` twice, separately, before either
batch is approved" -- every cooldown test up to that point called it once,
or twice back-to-back in the same test (which only proves the single-batch
case). It's now fixed at both the draft-creation and send-time layers, with
tests that specifically bypass `create_drafts()`'s own prevention to prove
`send()` holds the line independently. 116 tests total.

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

### An adversarial review pass found a genuine concurrency bug

I asked for (and ran) a structured, skeptical review aimed specifically at
breaking the three named guardrails -- different orderings, repeated runs,
state changing mid-flow, concurrent processes, and `quotes.json` vs. the
event stream disagreeing -- with every claimed finding backed by an actual
script against real data, not just reasoning about the code. It found one
genuinely serious bug and three smaller real ones:

- **Critical: the cooldown guardrail was bypassable under true concurrency.**
  `send()`'s cooldown re-check reads committed state and then writes based
  on what it read, across several separate statements. Two *real* OS
  threads, each with their own connection to the same file, calling
  `send()` for two different messages to the same customer at the same
  time, could both read "no recent contact" before either had written
  anything -- a textbook check-then-act race. Reproduced deterministically
  by forcing the interleaving with a `threading.Barrier`: both sends
  succeeded, two real outbox rows for the same customer. A contrasting test
  against the *other* guardrail (one open draft per quote) -- the identical
  attack shape -- held, because that one is backed by a real SQLite
  `UNIQUE INDEX`, not just an app-level check. That contrast is the fix:
  `send()` now wraps its guardrail re-checks and the outbox write in one
  `BEGIN IMMEDIATE` transaction, so a second concurrent sender blocks (via
  `busy_timeout`) until the first fully commits, then correctly sees it.
  Verified with 10 trials of genuine concurrent threads (no artificial
  synchronization) -- 1 outbox row every time, down from 2 before the fix.
- **`quote_accepted` events were parsed, stored, and completely ignored.**
  `quotes.json`'s `status` is a batch/snapshot field; a `quote_accepted`
  event (a type the seed data's own stream names explicitly) can arrive
  before a re-export catches up. A quote the event stream says was accepted
  could still surface as a candidate and get sent to. Fixed with
  `policy.quote_accepted_via_events()`, checked both when generating
  candidates and, independently, inside `send()`'s own re-check -- the
  event can arrive after approval too, not just before drafting.
- **`send()` used a phone number snapshotted at draft time**, not the
  current one. If a number is corrected between draft and send, the
  message went to the stale number -- inconsistent with quote status
  already being re-checked live at send time. Now reads the phone live too.
- **A DB trigger firing at send time could crash raw.** The
  `block_send_for_non_open_quote` trigger is a backstop for exactly the
  race above; when it fired, the resulting `sqlite3.IntegrityError` wasn't
  caught anywhere, so it would have surfaced as a traceback instead of a
  clean guardrail message. Caught now, consistent with how the equivalent
  triggers in `create_drafts()`/`approve()` are already handled. (The
  `BEGIN IMMEDIATE` fix above also closes the specific race that used to
  trigger this, but the catch stays in as a backstop for the backstop.)

Also found, lower severity: the README's own run instructions broke for a
checkout not literally named `followup_engine` (the repo root and the
Python package inside it share that name, and `cd followup_engine` is
ambiguous between them) -- confirmed by cloning into a differently-named
directory and hitting `ModuleNotFoundError` on the very first command, and
separately confirmed live when `<message_id>` in a copy-pasted command
caused an actual shell parse error during manual testing. Both fixed: the
README no longer tells you to `cd` into the package directory, and the
placeholder no longer uses shell-special characters.

All of this is covered by new tests (`TestConcurrentSendDoesNotBypassCooldown`,
`TestSendUsesLiveQuoteState`, `TestQuoteAcceptedViaEvents`), 123 total.

## Where I stopped

The initial MVP (ingest, policy, drafting, draft/approve/send, CLI, first
pass of tests and README) was built to the stated ~3-hour scope in one
sitting. Everything after that -- several audit passes, the phone/channel/
approver/concurrency additions, a coverage-and-correctness pass that found a
*sequential* cooldown bypass (two separate `draft` runs before either was
approved), and finally a structured adversarial review that found the
*concurrent* one (two simultaneous `send()` calls) plus the ignored
`quote_accepted` events -- was additional, explicitly-requested work: I
asked for rounds of adversarial self-review ("double check the guardrails,"
"do another pass," "verify 100% that it works correctly," a scripted
skeptical-reviewer prompt), and kept going back in. That's a different mode
of work than the original time-boxed build, and it's why the project ended
up more thoroughly audited than a strict 3-hour reading of the brief would
produce on its own -- worth being upfront about, since the assignment is
explicit that the time budget matters and I don't want the depth of the
later passes to misrepresent how long the initial build actually took. I
stopped where I did because the remaining items (listed above) are genuine
next-iteration scope -- a real provider integration, draft editing,
per-shop config, a web UI -- not loose ends in what's already here; the
code, tests, and guardrails are in a state I'm prepared to defend as
correct and complete against the brief.

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
