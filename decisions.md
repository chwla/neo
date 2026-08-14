# Decisions

Plain-language notes on the choices made while building out the memory layer's test
coverage. Written so it's obvious later *why* something is the way it is, not just what
it is.

---

## 1. I wrote a plan before writing tests

**What:** The first deliverable is [tests/memory/TEST_PLAN.md](tests/memory/TEST_PLAN.md) —
a list of ~880 things worth testing — not a pile of test files.

**Why:** The memory layer is about 21,000 lines across 60+ files. Writing tests
file-by-file without a map means you find out at the end that you tested the easy parts
thoroughly and never touched the transaction boundary or the owner-isolation guarantees.
The plan makes the gaps visible up front.

**Trade-off:** It costs a session before any test exists. Worth it here because the plan
is also the progress tracker — each item has a stable ID (`NRM-12`, `MUT-04`) so work can
stop and resume without re-deriving what's left.

---

## 2. The plan is organised by module, then by tier

**What:** Seven tiers, from pure functions up to full end-to-end journeys. Within each
tier, one section per source file.

**Why:** Two reasons. First, when a test fails you want to know immediately which file
broke — module-shaped sections give you that. Second, the tiers encode a running order:
the pure-function tests are fast and catch data-corruption bugs, so they should exist
before anyone bothers with the slow HTTP tests.

**Alternative I rejected:** organising by user-facing feature ("remembering", "forgetting",
"recall"). Reads better, but it hides which code is untested — a feature can look covered
while a whole module underneath it has never been exercised.

---

## 3. I marked what already exists rather than starting from zero

**What:** 23 plan entries are marked `[x]` and point at the test file that covers them.

**Why:** There are already 51 passing tests, and they're good ones — they pin real bugs
that were observed in the running app (duplicate memories on restatement, "forget" silently
failing). Re-writing them would be waste, and deleting them would lose the regression
history.

**Note:** The counts don't line up one-to-one. Some existing tests pin the same behaviour
from several angles; some plan entries will need several parametrised tests. 23 plan items
covered by 51 tests is the honest reading, not a discrepancy.

---

## 4. Tier 0 is test infrastructure, and it's a blocker

**What:** Eight items covering fixtures, factories, a frozen clock, and fake providers.

**Why:** The previous test suite (about 12,000 lines) was deleted in commit `9071502`, and
that took `conftest.py`, `factories.py`, and six helper modules with it. The five surviving
test files work because they only test pure functions — no database, no fixtures needed.
Everything in Tiers 3 through 7 needs a migrated in-memory database, a bound owner, and
record builders. None of that exists right now.

**Consequence:** Nothing below Tier 2 can be written until Tier 0 is done. That's why it's
listed as its own tier rather than being folded into the first test file that needs it.

---

## 5. I test against real SQLite, not mocks, for anything schema-related

**What:** All the `SCH-*` constraint tests, and the repository and mutation tests, run
against an actual in-memory SQLite database with the real migrations applied.

**Why:** The schema in [app/models/memory.py](app/models/memory.py) does a lot of the
safety work — there are check constraints enforcing UUID format, payload shape by
sensitivity, confidence and importance ranges, and partial unique indexes preventing two
active records in one exclusive slot. A mock would happily accept all of it. The only way
to know those constraints actually fire is to make the database reject a bad row.

**Cost:** Slower than pure unit tests. Acceptable — SQLite in-memory is fast, and these are
the tests most likely to catch a real corruption bug.

---

## 6. Every constraint and every enum value gets a rejecting case

**What:** For each `CheckConstraint` in the schema, there's a test that writes a row
violating it and asserts the write fails.

**Why:** A constraint that was never tested is a constraint you don't know is there. It's
easy to write a `CheckConstraint` with a typo in the SQL that silently never matches
anything — the table still builds, the tests still pass, and bad data flows in for months.

**Same reasoning** applies to the enum check constraints: for each list
(`OUTBOX_EVENT_KINDS`, `RELATION_TYPES`, `DERIVED_TARGET_STATES`, and so on) the plan tests
that every valid value is accepted *and* that one invalid value is rejected.

---

## 7. Owner isolation gets its own tier, tested at every layer

**What:** Tier 7's `ISO-*` section re-tests owner isolation at the repository, mutation,
recall, index, outbox, maintenance, and API layers — even though each of those modules
already has isolation cases in its own section.

**Why:** This is the guarantee that matters most for a local personal assistant with
multiple profiles. One person's memories showing up in another's chat is the failure that
loses trust permanently. The schema tries to enforce it (owner-scoped foreign keys, an
owner-binding table, per-profile databases), but a single query missing its `owner_id`
filter defeats all of it.

**Deliberate redundancy:** Testing it once per layer means a new query added to any layer
has an obvious place where it should have been covered.

---

## 8. Privacy is tested by sweeping the whole database, not by checking one column

**What:** The `PRV-*` tests attempt to store prohibited or sensitive content, then search
*every table* for the plaintext.

**Why:** Sensitive content flows through more places than you'd guess: the record, the
candidate, the source excerpt, the operation's command payload, the outbox event payload,
the derived index documents, and the logs. Checking that `memory_records.canonical_payload`
is encrypted proves nothing about the other six. A content sweep catches the one you forgot.

---

## 9. Time-dependent behaviour uses a frozen clock, never `sleep`

**What:** Tier 0 includes a clock-freezing helper, and every expiry, tombstone, freshness,
retry-backoff, and lease test uses it.

**Why:** Tombstones last 30 days, recall freshness decays over a half-life, and outbox
leases expire on a timer. Testing any of that against the wall clock means either sleeping
(slow) or writing a test that passes today and fails at midnight (flaky). A flaky test in
this suite is worse than no test — people stop trusting the whole run.

---

## 10. Model-backed extraction is tested against fakes, never a live model

**What:** All the `EXT-*` and `EXC-*` tests use a scripted `FixtureExtractionModel` or a
fake HTTP transport.

**Why:** Neo's extraction talks to a local Ollama instance. A test suite that needs Ollama
running, with the right model pulled, is a test suite that doesn't run in CI and doesn't
run on a fresh clone. The scripted double also lets us test the failure modes that matter —
timeouts, malformed JSON, a "model not found" response — which you can't reliably trigger
against a real model.

**What we still test about the real provider:** the request shape it builds, the response
shapes it accepts and rejects, and the mapping from each HTTP failure to its error code.
That's the part that broke in deployment before (extraction pointed at an uninstalled
model), so it's worth pinning.

**Only the model is faked — nothing under it.** In `test_extraction_coordinator.py` the
scripted model hands back JSON, and from that point everything is real: real parsing, real
grounding, a real mutation kernel, a real SQLite file. That's why those tests can process
the same turn twice and then *look at what actually landed* rather than at what the code
claims it did. Several assertions are written that way deliberately — "the status says
applied" and "there is one row in the database" are different claims, and the second is the
one that matters.

**Three doubles, not one** (`tests/memory/doubles.py`):

- `scripted_model` wraps the app's own `FixtureExtractionModel`, keyed by user message. A
  value can be a response, an exception, or a *sequence* — which is how the retry tests
  script "garbage first, valid JSON second".
- `RecordingModel` also captures what the model was shown. Some properties are about what
  the model is *not* given — it must never see an owner id it could echo back — and you
  can't check that without the input.
- `UnavailableModel` fails every call, with a switch for error vs timeout. This is the most
  realistic double of the three: on a machine with no Ollama installed, this *is* the
  provider.

**Spans are computed, never written by hand.** `doubles.source_span` finds the quoted text
in the message and derives the offsets, raising immediately if the quote isn't there. Spans
written by hand drift as soon as a fixture message is edited, and the resulting failure
looks like a grounding rejection — the test fails for a reason that has nothing to do with
what it was testing.

---

## 11. Prompt-injection resistance is tested with hostile content

**What:** `PMT-03` stores a memory whose display text contains the prompt header, a code
fence, and "ignore previous instructions", then asserts the serialised prompt still
contains it safely.

**Why:** Stored memories are user-supplied text that gets injected into a system prompt on
every turn. The serializer in [app/services/memory/prompt.py](app/services/memory/prompt.py)
wraps them in an untrusted-content block for exactly this reason. If a memory can close
that block, anyone who can get text into your memory can rewrite the assistant's
instructions — including via a webpage the assistant reads.

---

## 12. Performance tests are tripwires, not benchmarks

**What:** Five `PRF-*` cases with generous bounds — recall over 1,000 records finishes in
time, a mutation issues a bounded number of statements, recall doesn't N+1.

**Why:** The goal isn't to measure speed, it's to notice when something goes quadratic.
A test asserting "recall makes fewer than N queries" catches the day someone adds a
per-record lookup inside the scoring loop. Generous bounds mean it won't fail on a slow
laptop.

**Why not more:** Real benchmarking belongs in its own tool with its own baseline tracking.
Putting it in the test suite makes the suite slow and the numbers meaningless across
machines.

---

## 13. The older retrieval and context-compaction subsystems get a lighter pass

**What:** `app/services/memory_retrieval/` and `app/services/context_memory/` get about 21
cases between them — mostly "does the CRUD round-trip, is it scope-isolated" — versus
hundreds for `app/services/memory/`.

**Why:** They're smaller, simpler, and they ship in the app with their own routers, so they
need to work. But the canonical memory system is where the complexity and the risk live —
the transactional mutation boundary, the encryption, the derived-index reconciliation. The
effort should follow the risk.

**Asked and answered:** I flagged these as possibly superseded by the canonical memory
layer, and they're **not** — you confirmed both are live. So the 21 `RTV`/`CTX` items stay
in the plan and get written, at the lighter pass described above. Nothing gets deleted.

This is why I asked rather than assumed. The evidence from reading the code pointed the
other way — the canonical layer duplicates a lot of what they do — and if I'd acted on that
reading I'd have skipped coverage on two subsystems that ship with their own routers.

---

## 14. Gaps I find get recorded as failing tests, not silently fixed

**What:** While writing the normalization tests I found that the "don't store negated
facts" guard catches `do not want` and `did not want` but not `does not want`. I did not
change the source. Instead there's a test marked `xfail(strict=True)` that documents it.

**Why:** The task was to test the memory layer, not to change it. Quietly patching source
while writing tests means the test suite and the code change together, and nobody ever sees
the bug report. A strict `xfail` does three things: it records the finding in the place
you'd look for it, it keeps the suite green so a real regression still stands out, and it
turns red the moment someone fixes the pattern — which is the prompt to promote it into a
normal test.

**Eight found so far**, in order of how much they'd matter in real use:

| ID | What | How bad |
|---|---|---|
| **`PRE-01b`** | **"Call me Soham" has no deterministic pattern.** `policy._MEMORY_COMMAND` lists `call me` as an explicit memory instruction — and the existing test suite already asserts it opens the extraction gate — but `preparser._DIRECT_NAME` matches only `My name is X` and `I am called X`. Same for `I'm Soham`. | **User-visible.** The most direct way a person states their name falls through to the local model, when a two-branch regex already handles the *less* common phrasing deterministically. A name is the identity fact an assistant is asked for most, so it's the worst place to depend on a model call being available and correct. |
| **`RCL-21d`** | **The recall stemmer mangles `-es` plurals and doubled-consonant participles.** It strips a bare `s` but not the `es` plural, and doesn't undouble a final consonant. `sketches` → `sketche`, `running` → `runn` — neither matches `sketch` or `run`. | **User-visible.** These words aren't "left as written" as the docstring promises; they're transformed into forms that match nothing. It's the same failure mode as the "podcasting didn't match podcasts" bug this stemmer was written to fix. In this app specifically: "show me my sketches" reaches no memory that says "sketching". |
| **`RCL-31b`** | **`policy.USAGE_AFFECTS_RANKING = False` is declared, never read, and contradicted by the scorer**, which gives usage a 0.03 weight in the lexical total. | **Design contradiction.** Ranking by usage creates a feedback loop — recalled memories get recalled more regardless of relevance — which is exactly what the constant appears to guard against. The effect is bounded at 0.03 of total score, but that's enough to reorder near-ties, which is where ranking decisions actually get made. Either the scorer drops the term or the constant should say `True`. |
| **`EXC-19c`** | **A "forget" matching several memories removes only the first, and reports success.** `_apply_retraction` derives its idempotency key from `(owner, message_id, extractor_version, retraction.proposal_id)` — no target — but is called once per target in a loop. Every target computes the same key, so the second forget replays the first operation's record, finds it names a different memory, and returns FAILED. Both decisions carry the same `operation_id`. | **The most serious finding after SCH-14, and arguably worse in kind.** SCH-14 lets bad state exist; this *fails a deletion the user explicitly asked for and tells them it worked*. The comment above the loop says it was written to fix exactly this symptom — the loop was added, the key was not made per-target, so the original bug survives. The two interact: SCH-14 makes duplicate values more likely, and this makes them harder to remove. |
| **`RTV-12`** | **Workspace retrieval is not scope-isolated.** Both guards in `MemoryRetriever.retrieve` use `and` between the scope-type and scope-id mismatch conditions, so an item is skipped only when **both** differ. Any two scopes sharing a `scope_type` — every pair of chats — see each other's items. *(Found by the parallel session; I verified it independently at `app/services/memory_retrieval/retriever.py:13-21`.)* | **An information boundary, not a correctness bug.** This subsystem stores user-pasted transcript content, so the leak is between one chat and another. Comparable in severity to SCH-14 and EXC-19c, and cheaper to fix than either: the guards need `or` instead of `and`. |
| `RTV-09` | Renaming a workspace item returns 500. *(Parallel session's finding.)* | **User-visible but contained** — a failed rename, not lost data. |
| **`SCH-14`** | **The unique index guaranteeing one active record per exclusive slot does not fire for globally-scoped memories.** The index covers `(owner_id, scope_type, scope_project_id, subject_key, memory_type, domain_key, slot_key)`. Every global record has `scope_project_id IS NULL`, and SQL unique indexes treat NULLs as distinct — so two rows identical in all six other columns are not duplicates as far as the index is concerned. | **Serious.** This is the "one answer per question" guarantee, and it's off for your name, every preference, every current primary goal, current job, and current education. Two contradictory active records can coexist and recall returns whichever ranks higher. Project-scoped records *are* protected, which is what pins the cause. |
| `CON-21b` | An `UpdateMemoryCommand` cannot survive `model_dump(mode="json")` → re-parse. A full dump writes `canonical_value: None`, which `MemoryUpdatePatch` rejects; `exclude_unset` drops the `operation` discriminator instead. Eight of nine commands round-trip; only `update` doesn't. | **Moderate, and reachable.** `mutations.py` stores exactly this dump in `memory_operations.normalized_command_json`, and `execute()` re-parses dicts through the same adapter. So the audit record of an update can't be replayed through the front door that wrote it. |
| `POL-15e` | The sensitive-content address pattern matches a house number followed by `street/road/avenue/lane/drive/boulevard` — but not `Way`, `Court`, `Place`, `Terrace`, `Crescent`, `Close`, `Square`, or `Parkway`. | **Privacy gap.** A home address on a Court or a Way classifies as NORMAL, so it can be stored without the explicit request a home address is meant to require. |
| `NRM-30b` | The negated-fact guard catches `do not want` and `did not want` but not `does not want`. | **Minor.** Display text is normally the user's own first-person words, so this needs a model-written display hint to trigger. |
| `SCH-11b` | The "display text must not be blank" check uses SQLite `trim()`, which strips spaces only. A tab- or newline-only display text passes. | **Cosmetic**, but the constraint doesn't mean what it looks like it means. |
| `EXT-21d` | The provider resolves its response timeout with `response_timeout_seconds or timeout_seconds or 120`. Zero is falsy, so it's replaced by the default *before* the 1–600 range check runs. Every other out-of-range value, including `-1`, is caught. | **Minor**, and the substituted value is the sane default so nothing breaks. Reachable though: `MemorySettings.__post_init__` validates the input-char and recall limits but neither extraction timeout, and `factory.py` passes the setting straight through — so `0`, the natural way to write "no limit", silently means 120 seconds instead of raising. Recorded because a validator that misses exactly one value invites trust it hasn't earned. |
| **`OBX-15`** | **A worker whose outbox lease was reclaimed raises out of `process()` instead of reporting a failure.** `_finish_target` raises `lease_lost` when the delivery row no longer matches the worker; the `except` handler in `process()` computes the correct `DerivedFailureCode.LEASE_LOST` and then calls `_finish_target` **again**, which raises the same error with nothing left to catch it. | **Serious for availability, harmless for data.** `_failure_code` has an explicit `lease_lost` branch, so this was plainly meant to be a reported failure — it is unreachable. A stale lease isn't exceptional; it's the normal outcome whenever work outruns its lease, which is the exact situation leases exist for. The worker dies instead of recording the failure, and because `process_batch` maps over every lease, one stale lease aborts the rest of the batch too. Nothing is corrupted (the reclaiming worker already did the write) but the queue stops draining until restart. |

None of these are fixed. Each is a strict `xfail` that turns red the moment someone fixes
it, which is the signal to promote it into a normal test.

**On `SCH-14` specifically — confirmed against your actual profile databases:**

Migration `0003_memory_scopes` drops the original index and recreates it with the two new
scope columns included. Adding a nullable column to a unique index silently disables that
index for every row where the column is NULL. That's a general SQL trap rather than a
careless mistake, but it means the guarantee has been off since scopes shipped.

The two profile databases in `profiles/accounts/` make the cause unambiguous:

| Profile | Migrations applied | Index state |
|---|---|---|
| `1515a663…` | 0001, 0002, **0003** | includes `scope_project_id` → **not enforced at global scope** |
| `30c07278…` | 0001, 0002 | no scope column → **enforced correctly** |

The pre-0003 database still has the working index; the migrated one doesn't. Same schema
lineage, one migration apart.

**Good news:** I checked the migrated profile for records that already violate the
guarantee and found **none** — it only holds 3 records, so the window hasn't caused damage
yet. That matters for the fix: re-adding a real constraint fails if data already violates
it, so the ordering is *check for duplicates first, then fix the index*. Right now there's
nothing to clean up first.

I only read those databases — no writes, no deletions.

**Decided: pin it, don't fix it.** You chose to keep `SCH-14` as a strict `xfail` rather
than have me write migration 0004. That's the right call for this task — rebuilding a
unique index against a live database is a different job with a different risk profile than
writing tests, and the finding is now recorded in a form that turns red the day it's fixed.
The ordering note above still stands whenever it is picked up: **check for duplicate active
records first, then rebuild the index**, because a real constraint fails against data that
already violates it. Today there's nothing to clean up first.

One consequence worth naming: because the constraint doesn't fire, the store *can* reach a
state with two active records in one exclusive slot. That makes `EXC-19` (a retraction
resolving to many targets forgets all of them) testable after all — I can insert that state
directly and check the retraction clears both. It isn't a hypothetical state; it's one your
schema currently permits.

---

## 15. Existing behaviour is pinned as-is, even where it looks surprising

**What:** A few cases (for example `NRM-15`: owner id doesn't change a non-keyed
fingerprint) test the current behaviour explicitly rather than asserting what "should"
happen.

**Why:** When reading code you can't always tell whether something is a deliberate design
choice or an oversight. Pinning it means a future change to it is a visible, deliberate
decision rather than a silent one — the test fails, someone reads it, someone chooses.

**Where it applies, it's called out in the plan** so it isn't mistaken for an endorsement.

---

## 16. Where I substitute a fake, I pick the narrowest seam that still proves something

**What:** The plan called for a fake embedding provider (`INF-07`). I built
`StaticDuplicateFinder` instead, which stands in one level *higher* — at the
duplicate-finder callable rather than at the embedding model.

**Why:** The coordinator doesn't know embeddings exist. All it depends on is a callable
that answers "is there an existing record this candidate restates?" with a memory id or
nothing. Faking at that seam tests the policy questions that actually live in the
coordinator — which records are even eligible for comparison, what happens to the slot key
afterwards, what happens when the finder throws — without a vector model in the loop.

Faking one level lower would have tested the same policy *plus* the arithmetic of cosine
similarity, and I'd have had to hand-write embedding vectors to make the arithmetic come
out right. That's a test that fails when the similarity metric changes, which isn't what it
was written to check.

**What this leaves undone, stated plainly:** the similarity *threshold* — the behaviour at
0.929 versus 0.931 — isn't covered by this, and can't be. It still needs the
fixed-dimension embedding fake, which the vector-index tier needs anyway. `EXC-14` is
marked `[~]` rather than `[x]` in the plan for exactly this reason.

---

## 17. Two failure modes I found and deliberately did not flag as bugs

While covering the coordinator I found two behaviours that look wrong at first and are
right on inspection. Both are now pinned with the reasoning attached, because the next
person to read them will have the same first reaction.

**A failing duplicate finder does not stop the write.** If the embedding model is down, the
coordinator swallows the error and stores the memory. Silently ignoring an exception is
normally a smell. Here the alternative is worse: the choice is between possibly storing a
near-duplicate and definitely losing a fact the user asked to keep. A missed duplicate
leaves the store exactly as it would have been if the check didn't exist. A lost memory is
a promise broken.

**"I no longer use Python" archives; "Forget that I use Python" forgets.** Two paths, two
outcomes, and it would be easy to read that as an inconsistency. It isn't. The first is the
user telling you the world changed — the record stops being recalled but the history of
what was true stays intact. The second is the user asking for the fact to be gone, which
forgets it and writes a tombstone so it can't come back. Collapsing them either destroys
history nobody asked to lose, or quietly fails to honour a deletion request.

There's a third property worth naming here: **"Forget that X" is handled with no model at
all.** It goes through the deterministic preparser, so deletion keeps working on a machine
where Ollama isn't installed. Being unable to *add* a memory when the model is down is an
inconvenience. Being unable to *remove* one would not be acceptable.

---

## 18. Working rules from CLAUDE.md, and what changed because of them

`CLAUDE.md` sets four rules: think before coding, keep it simple, make surgical changes,
and work to verifiable goals. Three things changed when I checked the work against them.

**I had been running `ruff format` across the whole `tests/memory/` directory.** That
reformatted two files I didn't write — `test_forget_and_duplicates.py` and
`test_semantic_duplicate.py` — reordering imports, rewrapping lines, and deleting an unused
import. None of it traced to anything asked for. Rule 3 is explicit that unrelated dead
code gets *mentioned*, not deleted. Reverted, and from here formatting is scoped to the
files I actually touched.

*(Mentioning rather than deleting, as the rule says: `test_forget_and_duplicates.py`
imports `UUID` and only uses it in an annotation that `from __future__ import annotations`
makes lazy. Harmless. Left alone.)*

**`INF-06` was marked done when nothing used it.** The frozen-clock helper exists and works,
but no test consumes it yet — every test written so far reads a timestamp rather than
advancing one. Claiming `[x]` for it inflated the progress number by exactly the amount
that makes a plan untrustworthy. Now `[~]`, with its real consumers named.

**Some assertions were hedged.** Several tests were written as "the status is one of
these three" or "if this field is set, it equals X". That passes whatever the code does,
which is the opposite of pinning behaviour. I ran the coordinator against real inputs,
read the actual values, and rewrote them as exact assertions — `[IDEMPOTENT_REPLAY]`, not
"one of three actions"; `stored == {"Rust", "Go", "Elixir", "Swift"}`, not "four things
were kept". A test that can't fail isn't a test.

**Where I'm not applying rule 2 literally:** the docstrings in these test files are long,
which reads as excess against "minimum code that solves the problem". The rationale is
that the second half of this task is explaining decisions in plain terms, and the honest
place for "why is this test asserting something that looks wrong" is next to the assertion
rather than in a document nobody opens. The test *code* is kept minimal; the prose is the
deliverable.

---

## 19. The HTTP layer is tested by faking the socket, not the provider

**What:** The `EXT-*` tests inject a scripted `FakeTransport` into the real provider
classes, rather than replacing the providers themselves.

**Why:** `JsonHttpTransport` is a Protocol and every provider already takes one as a
constructor argument, so this seam exists in the production code — it isn't something the
tests bolted on. Substituting there means the payload construction, the envelope decoding,
the error classification and the message sanitisation all run for real. The only thing
that doesn't happen is the packet leaving the machine.

Faking one level up — a stubbed `OllamaChatExtractionProvider` — would have tested nothing,
because the provider *is* the thing under test. That's the mistake this seam avoids.

**What this buys, concretely:** 69 tests that need no Ollama, no model pulled, and no
network. They run in 0.2 seconds. This matters more than usual here: the one deployment
failure this layer has actually had was extraction pointed at a model that was never
installed, and a test suite that itself requires an installed model could never have caught
it.

**The failure that shaped the tests.** Ollama reports some errors with HTTP 200 and an
`error` key in the body, and reports streamed output as newline-delimited JSON where every
line parses on its own. Both look like success to anything checking only the status code or
only "did JSON parse". Each has its own test and its own error code, because each has a
different fix — pull the model, versus set `"stream": false`.

**One deliberately paranoid case:** a response whose message claims `role: "user"`. Accepting
it would let provider-authored text enter the pipeline as though the user had typed it,
which is precisely what grounding exists to prevent. The provider rejects it, and now
something says so.

---

## 20. The derived indexes get tested as a leak surface, not as a search feature

**What:** The `IDX-*` tests spend more effort on what *never* reaches the index than on
what searching returns.

**Why:** The FTS table stores `display_text` verbatim — it has to, because you cannot run
a full-text query over ciphertext. Everything else in the memory layer protects a sensitive
value by encrypting it; this path can't. So the only protection is the builder refusing to
produce a document at all, and that single `if` is the whole guarantee.

The test over lifecycle states is written as a loop over the enum rather than a few
examples, for the same reason: adding a new state without adding it to the builder's check
would leave records in that state searchable, and a hand-listed test wouldn't notice.

**The staleness guard is the other half.** Deletes arrive from a queue and can be delayed.
If a memory is updated between a delete being enqueued and processed, an unconditional
delete removes the *new* row on the strength of a decision made about the old one. Both
indexes take an expected hash and refuse when it doesn't match — there's a test for the
refusal and a test for the matching case, because a guard that always refuses is as broken
as one that never does.

**Two tables, one delete.** FTS keeps a metadata row and a separate FTS5 virtual-table row.
Clearing only the first would leave the text searchable while the index believed it was
gone. That gets its own test on both the single-row and clear-owner paths, because the
failure is invisible from the metadata side.

## 21. Two things I expected to be bugs and aren't

**`_cosine` returns 0 for mismatched vector lengths instead of raising.** My plan
(`IDX-22`) assumed it should raise — comparing vectors of different dimensions is
meaningless. On reading where it runs, returning 0 is better: it's inside a loop over every
stored vector, so one row written by a previously-configured embedding model would abort
the entire search rather than just failing to match. Scoring it 0 excludes it, which is
exactly what you want from a vector that can't be compared. The mismatch *is* caught where
it can be acted on — `upsert` raises `embedding_dimension_mismatch`, so a wrong-dimension
vector can't be stored in the first place. Test renamed and the reasoning written down.

**A slot rename doesn't invalidate the embedding.** The derived hash and the embedding hash
cover different material, which looks like an oversight until you notice re-embedding is
the expensive operation here. The derived hash asks "has anything about this memory
changed?"; the embedding hash asks "would this embed differently?" Only the text can change
an embedding. There's now a test asserting both at once: renaming the slot moves one hash
and not the other.

## 22. `INF-07` is finally done, at two seams rather than one

Decision 16 explained why the duplicate finder was faked at the callable rather than at the
embedding model, and flagged that a fixed-dimension embedding fake was still owed for the
vector index. That now exists: `FakeEmbeddingProvider` derives vectors from a hash of the
text, so identical text always embeds identically (a re-index is genuinely a no-op) and
different text embeds differently (two memories can't collide). Tests that need a specific
geometry — orthogonal, identical, opposite — pass explicit vectors instead.

Both seams were needed. Neither would have done the other's job.

---

---

## 23. The outbox is tested as a queue, not as a function

**What:** Every `OBX-*` test drives the clock explicitly and asserts on database rows rather
than return values alone.

**Why:** Canonical writes commit first; the derived indexes are updated afterwards by a
worker draining this queue. That gap is deliberate — a slow embedding model must never hold
up a user's write — but it means the whole surface is "work happens later, elsewhere, maybe
twice". Testing `process()` as a pure function would miss every property that actually
matters: whether a second worker can steal a held lease, whether a crashed worker strands a
delivery forever, whether a retry storm is bounded.

This is where the frozen clock finally earns its place. Lease expiry, exponential backoff
and the dead-letter threshold are all time-dependent. Against the wall clock these would
need `sleep` (slow) or would pass now and fail at midnight (flaky). Decision 9 said a flaky
test here is worse than no test; this is the section that would have been flaky.

**Boundaries are tested on both sides.** A lease is reclaimed at 61 seconds and *not*
reclaimed at 59. A failed delivery is not leasable before its backoff elapses and is
afterwards. A guard that always fires is as broken as one that never does, and only the
pair distinguishes them.

## 24. A test that was passing without testing anything

`OBX-27` (idempotency) was originally written as "process the event, then try to lease it
again, and if you get a lease process it too". I checked what the second lease actually
returned: nothing. Both deliveries were already terminal, so the `if` never ran and the test
asserted idempotency without ever processing anything twice.

Rewritten around how double processing genuinely happens: a lease expires while its work is
still running, another worker reclaims and completes it, and the original worker then
finishes too. That is a real at-least-once scenario rather than an imagined one — and
writing it that way is what exposed `OBX-15` below.

The general lesson, and the reason this gets its own section: **a conditional inside a test
is a place where the test can quietly stop testing.** If the branch is the interesting case,
the test should fail when the branch isn't taken, not skip it.

## 25. The tenth finding, and why it was invisible until now

`OBX-15` — a worker whose lease was reclaimed raises `RuntimeError("lease_lost")` out of
`process()` instead of returning a result carrying `DerivedFailureCode.LEASE_LOST`.

The give-away is that `_failure_code` contains an explicit branch mapping the message
`lease_lost` to that code. Somebody intended this to be a reported failure like every other.
It cannot be: the `except` handler computes the code, then calls `_finish_target` a second
time to record it — and `_finish_target` is exactly what raised in the first place, because
the lease is still lost. The second raise has nothing catching it.

Why no earlier test found it: reaching this state requires a lease to expire *and* be
reclaimed *and* the original worker to keep going. With a real clock that's a timing
accident; with a frozen clock it's three lines. The infrastructure decision from §9 is what
made the bug reachable.

Recorded as a strict `xfail`, alongside a passing test pinning the current behaviour, so
both what it does and what it should do are written down.

## 30. Numbering starts at 30 because two sessions are writing this file

Sections 1–22 belong to the session working Tier 4 forward. I picked up the items its
progress table listed as open — `VER`, then the `PRE` and `COR` gaps — and I number from
30 so neither of us has to guess whether 23 is taken. Same reason the two of us stage
explicit file paths rather than `git add tests/memory/`: we share one working directory,
so a directory add sweeps up whatever the other session happens to have half-written.

## 31. The version constants are tested as a set, not as fifteen strings

`versions.py` is constants and nothing else, which makes it look like there is nothing to
test. What it actually holds is the compatibility story: every command carries
`contract_version`, every derived document carries the builder version that produced it,
and each of those is compared for equality somewhere else in the layer.

That framing picks the tests. A blank constant turns an equality check into one that always
passes. Two constants sharing a value make them indistinguishable — bump one and you either
silently invalidate the other's data or silently fail to invalidate it. Neither failure
raises anything; the wrong data is already written by the time it shows up. So the tests are
about the constants as a group, and they enumerate the module rather than listing names,
because the case that matters is the sixteenth constant somebody adds next year.

**I dropped an exact-count assertion after it failed.** VER-01 originally asserted the
module holds exactly 15 constants. It holds 14. The obvious fix is to change the number,
and it is the wrong fix: a hardcoded count fails on the entirely legitimate act of adding a
constant, and a test that cries wolf teaches people to bump the number without reading why
it moved. What the assertion was really there for is to stop the parametrised tests from
passing vacuously if the constants were ever moved or renamed out from under them.
Membership of the names the rest of the layer imports does that job and stays quiet when a
constant is added.

## 32. `VER-03` needed a second seam to test what it claims

The plan describes VER-03 as proving the `Literal[CONTRACT_VERSION]` guard actually
rejects. Every command is built through a factory in Python, so it always gets the right
default — the guard is unreachable from the normal path and only bites on a command that
arrives as a *dict*, from a replay envelope or a stored payload, which is exactly where a
version from an older build turns up. So the test mutates a dumped payload rather than
constructing a command.

Routing that through `MEMORY_COMMAND_ADAPTER` only reaches `contract_version`. The other
two guards live on `CandidateProposal`, and they guard different things — `taxonomy_version`
covers how a slot was built, `policy_version` covers which sensitivity rules classified it.
A proposal carrying a stale one of those is a different kind of wrong from a stale contract
version, so each is pinned separately.

There is also a test asserting the *unmodified* payload still validates. Without it, the
rejection test would keep passing if the factory ever started producing something
unparseable for an unrelated reason — it would be rejecting for the wrong cause and looking
identical from the outside.

## 33. The preparser tests pin what it *classifies*, not what its pattern names imply

The file's existing docstring already warns that several patterns exist but hand off to
the model anyway. Writing the remaining fifteen items turned that from a caveat into the
main finding: three of the plan's own descriptions were wrong about what the code does,
and in each case I pinned the code and corrected the plan rather than the reverse.

**`PRE-08` is the clearest.** The plan called `_NOW_GOAL` "goal, correction-flavoured".
It actually returns AMBIGUOUS. That is right: "Now I want X" implies something is being
superseded but names no predecessor, so acting on the implication means superseding
whatever happened to occupy the slot. The pattern hands it on instead. I marked the item
`[x]` against the real behaviour and rewrote the plan line, because a plan that describes
a behaviour the code doesn't have is worse than one that says nothing.

**`PRE-07` hides a distinction in a field nobody looks at.** "I want to make YouTube
videos" and "I want to travel more" both return `DETERMINISTIC_ASSERTION`. The difference
is the `deterministic` flag — False for the second, because with no domain there is no
slot to write to. Asserting only `kind` would pass for both and prove nothing, so the
flag is asserted directly.

**Where I checked behaviour before writing expectations.** All fifteen were run through
the real `preparse` first and the tests written against the output. Several of my initial
guesses were wrong — I expected `_ADDITIVE_GOALS` to strip the leading "to" from its
values (it does not) and expected the compound scanner to fire on a single correction
pair (it requires two, and falls through to the single-correction grammar). Guessing and
then adjusting until green would have produced the same passing suite while pinning my
guesses instead of the code.

## 34. One inconsistency found and deliberately left alone

`_NOW_GOAL` does not run its value through `_video_verb`; every other goal path does. So
"Now I want to make short films" stores `make short films` while "I want to make short
films" stores `create short films` — the same goal under two strings, which would not
match on a later retraction.

I did not fix it, for the reason decision 15 gives: this suite pins existing behaviour.
There is also a real argument it doesn't matter — the path is AMBIGUOUS, so the value is
a hint to the model rather than something stored directly. But that argument depends on
the classification never changing, which is exactly the kind of assumption that stops
being true quietly. There is now a test asserting the current value, so anyone who makes
that path deterministic will find this decision waiting for them.

The matching positive case is pinned too: a goal stored via `I want to make …` and
retracted via `I no longer want to make …` fold to the same string. That is the property
`_video_verb` exists for, and nothing tested it before.

## 35. `COR-12` was wrong about the code, and the code is right

The plan said `_domain_for` "fails closed when neither grounds a domain". It does not: it
returns `global`. I pinned the real behaviour and rewrote the plan line.

The interesting part is that both behaviours exist, one layer apart. `resolve_domain`
genuinely does fail closed — TAX-04 pins that there is deliberately no last-token fallback,
because inventing a domain from whatever word happened to come last is how a fact ends up
filed somewhere nobody will look for it. `_domain_for` then *catches* that refusal and
defaults to `global`.

That is not the lower layer being undermined. The two are answering different questions.
`resolve_domain` is asked "what domain does this text name?" and correctly refuses to
guess. `_domain_for` is asked "where should this fact live?", and at that point the choice
is not between the right domain and the wrong one — it is between filing the fact under
`global` and discarding a durable fact the user actually stated because a label didn't
parse. A domain is an organising facet; the fact is the thing worth keeping.

The safety property that "fails closed" is really protecting is that the memory layer never
stores something the user didn't say. That is still enforced, just not here — the *value*
must be grounded in the user's own words, and `ground_assertion` checks it independently of
the domain. Losing the label costs recall precision; losing the grounding check would let
invented content in. Only one of those is a correctness boundary.

## 36. A weak assertion of mine, found by the other session's warning

The session working Tier 4 hit a test that had silently stopped testing: an `if` inside it
was never taken, so the assertions under it never ran. It suggested checking the PRE/COR
work for the same shape. One case:

```python
assert result.retractions == () or all(
    "watercolour" not in span.normalized_value for span in result.retractions
)
```

The left side was always true, so the right side never evaluated. It would still have
caught the regression it was written for, but it conflated two very different outcomes —
"the pattern correctly declined to match" and "the pattern matched and stayed bounded" —
and passed on either. Replaced with the actual behaviour: a second sentence defeats the
`fullmatch` outright, so the turn goes to the model rather than being extracted with a
target stitched across sentences.

A disjunction in an assertion is worth the same suspicion as a conditional. Both mean the
test accepts more than one world, and usually only one of them is the one you meant.

## 37. I recount the plan's table instead of editing its numbers

Two sessions editing hand-maintained totals in one file will drift within the hour, so the
table is regenerated by counting the file's own checkboxes between `## Tier` headers.

My first counter reported 862 items against a stated 880. The instinct is to write off 18
as a stale denominator. It was my regex: item IDs were matched with `[A-Z]{3}-`, and `E2E`
has a digit in the middle, so every end-to-end journey item was invisible. `[A-Z0-9]{3}-`
reconciles it to exactly 880.

Worth recording because the failure was silent and self-consistent — a table built from
that count would have looked plausible, been internally consistent, and quietly understated
the remaining work by the entire E2E section. The check that caught it was comparing the
generated total against a number derived a different way, which is the only reason to keep
the stated denominator around at all.

---

## 26. Reconciliation's checkpoint is tested as a pure function first

**What:** Ten of the `MNT-*` tests never touch a database. They exercise
`_parse_reconciliation_checkpoint`, `_format_reconciliation_checkpoint` and `_next_cursor`
directly, before anything that uses them.

**Why:** The checkpoint is three independent cursors packed into one opaque string that
crosses a process boundary — a caller stores it and hands it back on the next pass. If
parsing and formatting disagree even slightly, resumption either fails loudly (fine) or
silently restarts (not fine, because a re-scan reports success). Testing that through
`reconcile()` would mean a database round-trip per case and would make a parsing bug look
like a reconciliation bug.

Two cases are there specifically because they cause infinite loops rather than wrong
answers: a page of exactly `limit` items must be marked done (a resumable cursor would
schedule a pass that finds nothing and returns the same cursor forever), and a malformed
checkpoint must raise rather than being read as "start from the beginning".

## 27. The loop I wrote first would have re-scanned forever

Worth recording because it is a contract that reads backwards.

`reconcile()` signals completion by returning `next_checkpoint = None`. I wrote the
resumption loop to continue until `checked == 0` instead, feeding the returned checkpoint
back in each time. Passing `None` back does **not** mean "carry on from where you were" —
it means "start from the beginning". So the walk completed correctly in three passes and
then started over, and my test counted 17 records in a store of 5.

The code was right; my loop was wrong. But the failure mode is bad enough to deserve its
own test: a caller who makes this mistake gets a reconciliation that never terminates and
never reports an error, just permanent CPU. `MNT-12b` now pins `next_checkpoint is None` as
the termination signal and asserts that passing `None` restarts, so the behaviour is
written down rather than inferred.

## 28. Two more expectations that were wrong about the code, not the code being wrong

**`PrivilegedGlobalMemoryMaintenance` refuses at construction, not per method.** My plan
item (`MNT-22`) assumed each method checks authorization. It doesn't — the constructor
raises. That is stronger: an unauthorized instance cannot be brought into existence, so
there is no object to accidentally pass somewhere that trusts it, and no method can forget
to ask. Test rewritten around what it actually does.

**`verify_owner_rebuild` takes the `RebuildResult` it is verifying.** I had assumed it
re-derived state from the database. Passing the result in is what lets it check
`result.owner_id` against its own owner and refuse a mismatch — verifying one profile's
rebuild against another's index would compare unrelated stores. That refusal now has its
own test (`MNT-16b`).

Both were cases where the plan encoded an assumption made from the module's shape rather
than its source. Pinning what the code does, and saying why it is defensible, is more
useful than filing a defect against my own guess.

## 38. `COR-18` asked the wrong layer, and found dead code doing it

The plan expected a refined value on an occupied exclusive slot to resolve to `REFINE`
rather than `CREATE`. It resolves to `NEEDS_REVIEW`, and `resolve` never returns `REFINE`
at all — nothing in the module does.

Refinement is a *planning* decision, not a resolution one. The resolver answers "which
stored record, if any, is this candidate about?" from a list of snapshots. Whether the new
value is a compatible refinement of the old one is decided in `planner.py` against the
record itself, and `PLN-03` already pins it (`MemoryOutcome.REFINED`). Two different enums,
two different layers, and the plan item conflated them.

That leaves `CorrectionResolutionKind.REFINE` as an unused enum member. I flagged it rather
than deleting it: it is pre-existing, removing it is a source change, and this suite's job
is to describe the code rather than tidy it. There is now a test asserting the current
outcome is NEEDS_REVIEW and explicitly *not* REFINE, so if someone wires the member up
later the test will tell them what changed.

## 39. Where a guarantee lives matters more than whether it holds

`COR-23` and `COR-24` said resolution ignores non-active snapshots and never targets
another owner's record. Neither is true of the resolver: `resolve` reads neither `status`
nor `owner_id`, and `resolve_retraction` doesn't either. Hand any of them an archived or
foreign snapshot and it will match it — on the retraction path, it comes back as a delete
target.

This is not a defect, and I want to be precise about why. The resolver is a pure function
over the records it is given. The filtering happens in `MutationService.list_active_records`
— ACTIVE only, against an owner-scoped repository — which `MUT-34` already pins. The system
is correct as assembled.

But "the system is correct as assembled" is exactly the kind of claim that stops being true
when someone adds a second caller. The property everyone assumes is being enforced here is
in fact a *precondition* on the input, and preconditions that nobody writes down are the
ones that get violated. So rather than write a test that passes because the fixtures
happened to be clean — which is what testing the resolver for owner scoping would have
been — the tests assert the boundary as it really is: pass in an archived record and it
matches, pass in a foreign one and it matches.

A test that documents a trust assumption is worth more than one that pretends the
assumption isn't there. The next person to call this resolver now has to pre-filter, and
will find out why from a test rather than from a cross-owner bug.

## 40. Three of eleven plan items were wrong, which is itself the finding

COR-18, COR-23, COR-24 and (in a smaller way) COR-32 all described behaviour the code does
not have. That is not a criticism of whoever wrote the plan — it was written from module
names and function signatures before the code was read closely, which is the only way to
write a plan of this size up front.

It does mean a plan item is a hypothesis, not a specification. Every one of these was
resolved by running the real function first and writing the test against what came back.
Had I instead written the test the plan described and then adjusted until it went green, I
would have produced eleven passing tests pinning four behaviours the system does not have,
and the suite would have been actively misleading about the layer boundaries.

The tell, every time, was that the test was hard to write. `COR-24` needed a foreign
snapshot to be rejected, and there was no code path that could reject it. That difficulty
is information.

---

## 29. A precondition the other session found, checked against my layer

The parallel session covering the correction resolver found that `resolve` and
`resolve_retraction` read neither `status` nor `owner_id`. Hand either an archived or a
foreign record snapshot and it matches — on the retraction path, as a *delete target*.

That is not a defect. `list_active_records` filters to ACTIVE against an owner-scoped
repository, so the system is correct as assembled. But it means "only active, only mine" is
a **precondition on the input** rather than a property the resolver enforces, and an
unwritten precondition is exactly the kind that gets violated when a second caller appears.

Maintenance is a second caller, so I checked rather than assumed:

- It never calls the resolver at all — no `resolve`, no `resolve_retraction`.
- It enumerates through `repository.list_index_candidates`, which builds on
  `eligible_records_statement`: `owner_id`, `status == ACTIVE` and expiry are all filtered
  **in SQL**, before any Python sees a row.

So the concern doesn't reach this layer. `MNT-02c` now pins the status axis explicitly
(owner scoping was already pinned either side), so if someone later changes maintenance to
assemble its own record set, a test fails rather than a silent assumption breaking.

Worth recording as a pattern rather than a one-off: when a shared helper turns out to rely
on its callers having filtered, the useful response isn't only to document it where it
lives — it's to go to each caller and show, with a test, that the caller does its part.

## 41. The prompt serializer is the other half of grounding, and I named it that way

`test_grounding.py` guards the input end: text the user never wrote cannot *become* a
memory. `PMT-03` guards the output end: a stored memory cannot *escape* its container on
the way into a prompt. Same threat, opposite direction. Both halves have to hold, because a
fact can get in legitimately and still carry an instruction — the user may have been
quoting a webpage when they asked Neo to remember something.

**The guarantee is narrower than the plan's wording, and I pinned the real one.** The plan
said text is "escaped/fenced". What actually happens is that each record is wrapped in a
`<memory …>` element and its text is HTML-escaped, so `<` and `>` cannot produce a tag.
That is the entire mechanism. Backticks, newlines, and a verbatim copy of the header all
pass through unchanged, because `html.escape` touches only `& < > " '`.

Containment still holds — the delimiters are the escaped tags, so a copy of the header sits
inside a properly closed element and starts nothing. But "escaped/fenced" invites the
reader to believe the text was neutralised, and it was not. The tests assert tag counts
rather than absence of scary strings, and one test pins the header-copy case explicitly so
the limit is written down instead of discovered.

The instruction-shaped text is deliberately *not* stripped, which is worth defending: a
user may legitimately ask Neo to remember "my boss always says 'ignore previous
instructions'". Removing it would be censoring the user's own data. The design keeps the
text, contains it structurally, and tells the model to ignore instructions found inside.

**`PMT-01` turned out to pin a separation worth having.** `STABLE_MEMORY_POLICY` is not in
the serialized block at all — it goes in the system prompt, while `_HEADER` travels inside
the untrusted message. That is the right split: an instruction that lives only inside the
untrusted block is an instruction sitting next to attacker-controlled text.

**`PMT-14` is a third instance of the COR-23/24 shape.** The plan expected sensitive text to
be withheld here; `prompt.py` never reads `sensitivity`. Recall does the filtering
(RCL-07/08), and QRY-02 pins that the unlocking flag cannot even be set outside
deterministic mode. Three layers now, each holding one part, and none of them checking what
the others already did. That is good design and bad documentation, which is why each one
gets a test naming the layer that actually holds it.

## 42. I wrote the vacuous-test bug into my own work, one commit after describing it

Decision 36 recorded a disjunction that always passed on its left side. Two files later I
wrote:

```python
serialized = _serialize(_view("a" * 500), _view("b" * 500), characters=900)
if serialized is not None:
    assert len(serialized.content) <= 900
```

The header alone is 443 characters, so at a 900 cap with two 500-character records nothing
fits, `serialize` returns `None`, and the body never runs. The test passed while asserting
nothing at all — the exact failure the other session hit, and the exact one I had just
written up.

Knowing the failure mode is not the same as having a habit that prevents it. What actually
caught it was checking, after the file went green, which of the new assertions could ever
have failed. That check is cheap and I should have run it on the PRE work too rather than
being told.

The replacement is parametrised over two caps, chosen so the budget binds in one case and
not the other, and it asserts the record count so that "nothing was produced" can never
masquerade as "the budget was respected".

## 43. Two plan items asked for `None` where a refusal is better

`DAN-06` expected the direct-answer path to return `None` when no stored record answers the
question. It returns a sentence: *"I do not have an active saved memory that answers that
yet."*

`None` means "not mine, pass it on", and passing it on hands the question to the chat
model — which has the whole conversation in context and will often produce a confident
guess at your name. The explicit refusal is the stronger behaviour, because reliably saying
*I don't know* is precisely the thing a language model asked about your personal details
will not do. The distinction only matters in the case that matters: when the memory is
missing.

`CHT-07` is the same shape. The plan wanted a disabled memory setting to yield "a runtime
that injects nothing"; `build_chat_memory_runtime` returns `None` instead. That is better
for a reason worth stating: an inert runtime is an object a caller can still call, and one
that quietly returns empty results is indistinguishable from a working one that found
nothing. Returning `None` makes the disabled case structurally unmissable.

Both are cases where the plan specified the mechanism and the code chose a better one.

## 44. The trust-boundary pattern turned out to be structural, not accidental

`COR-23/24`, `PMT-14` and now `DAN-08/09` are four instances of the same finding: the plan
expected a layer to enforce sensitivity or owner scoping, and the layer does not read those
fields at all.

After the fourth I stopped treating it as a series of separate discoveries. The reason is
architectural: `CanonicalRecallService` is the only component below chat that touches the
database, so every consumer downstream of it — the serializer, the direct-answer path,
the chat wiring — inherits its filtering rather than repeating it. That is good design.
Repeating an owner check in four places invites the four copies to disagree.

What it costs is that the guarantee is invisible from any of the four call sites. So each
one now has a test naming the layer that actually holds it, and `DAN-09` gets a positive
assertion for the part that layer genuinely owns: it never *rewrites* the owner, which it
could easily do, since it builds a new query by copying the context to change the recall
mode.

## 45. Two tests of mine that could not have failed

Both found by auditing the new assertions after the file went green, which is now the step
I run before committing rather than the one I skip.

**A circular assertion.** The first version of `CHT-02` computed its expectation from the
same regex the code consults:

```python
expected = BROAD if _BROAD_MEMORY_QUERY.search(prompt) else SCOPED_LEXICAL
assert mode is expected
```

That holds no matter what `context_for` does with the match — it re-derives the answer
instead of stating it. Replaced with literal expected modes, which then forced me to
discover that one of my "specific" prompts wasn't: *"what do you remember about the api"*
contains the broad trigger phrase and enumerates. That is now its own test, recorded rather
than fixed — broad mode returns more than needed rather than less, so the cost is budget,
not a wrong answer.

**A comparison that held either way.** The freshness test asserted `second >= first` on two
real `now()` readings. If the timestamp were frozen at build time the two would simply be
equal and the assertion would still pass — testing nothing about the property it was named
for. It now stubs the clock to return successive instants and asserts both exact values.

The common thread with decision 42 is that all three passed for a reason unrelated to the
behaviour they described. A test that cannot fail is worse than a missing one: the missing
test is visible in the plan as an open item.

## 46. `degraded_lexical` means "tried and failed", not "switched off"

`RCL-53` expected `lexical_available=False` to produce `degraded_lexical=True`. The
behaviour is right and the flag is right; the plan conflated two different situations.

`_fetch` sets the flag in exactly one place: when the FTS query raises. A configured-off
lexical path returns `([], False)` instead. That distinction is worth keeping — "the search
engine fell over" and "this deployment does not run a search engine" want different
responses from whoever reads the diagnostic, and collapsing them would make the alerting
signal fire constantly on a semantic-only install.

What it costs is that the diagnostic no longer describes the disabled case at all: a
semantic-only result looks, from the outside, exactly like a normal hybrid one that
happened to find nothing lexically. I pinned the current behaviour rather than changing it,
because the flag's name matches what it does; but the gap is real and now written down.

`RCL-54` is the same kind of correction. The plan wanted both reason codes when lexical and
semantic are both unavailable. Recall short-circuits *before* `_semantic` runs, so there is
no semantic diagnosis to report and the result carries `LEXICAL_UNAVAILABLE` alone. The
part the plan actually cared about — no exception, empty result — holds.

## 47. I did not reuse the shared embedding double, and the reason is a real trap

`doubles.FakeEmbeddingProvider` returns a `ProviderHealth` object from `health()`. Recall
does:

```python
healthy = health()
if not healthy:
    diagnostic["degraded"] = "embedding_unhealthy"
```

`ProviderHealth` is a pydantic model with no `__bool__`, so it is *always* truthy. Passing
that double to recall makes the unhealthy branch unreachable: a test asserting graceful
degradation would still pass, because `embed()` would then raise and be caught by the
outer handler — producing `semantic_unavailable` instead of `embedding_unhealthy`. Right
outcome, wrong path, and the test would have been green while proving nothing about the
health check.

I checked whether this was a defect in recall before working around it. It is not: the
`EmbeddingProvider` protocol declares `health() -> bool`, and the production
`ValidatedMemoryEmbeddingProvider.health()` returns a real bool — it even unwraps a
structured result via `getattr(result, "healthy")`. Recall is correct; the double simply
conforms to a different contract than the one recall consumes, which is fine for the vector
index tests it was written for.

So this file defines its own provider stub returning a plain bool, with the reason in its
docstring. The general lesson: a shared double is only shared if every consumer needs the
same contract, and "it has the right method names" is not the same as "it has the right
return types".

## 48. Testing a filter means making the thing it filters unreachable by other routes

Three of these tests were initially unable to fail, all for the same reason. A record whose
display text is "improve at urban sketching" matches the query "urban sketching"
*lexically*, so it appears in the result whether or not the semantic path returned it.
Asserting it was present proved nothing about semantics; asserting it was absent failed
even when the semantic drop worked correctly, because lexical had put it back.

The fix is to give the record text that shares no token with the query — "plays the cello
on tuesdays". Then presence is evidence the semantic path contributed, and absence is
evidence it did not. The lexical route is closed, so only the route under test remains.

This generalises past this file. When testing that a filter drops something, the test is
only meaningful if every *other* way of obtaining the thing is closed off first. Otherwise
you are not testing the filter, you are testing whichever path happens to win.

## 49. The Ollama endpoint derivation is load-bearing, and I only found that by being wrong

I wrote a test asserting that a non-Ollama provider with no endpoint yields an empty
endpoint string. It doesn't — it raises. Following that failure produced the more useful
finding underneath it.

`from_settings` sets `live_extraction_model_enabled` from `memory_extraction_enabled`,
which is on by default. `__post_init__` then requires a non-blank endpoint whenever live
extraction is enabled. And the shipped defaults are `provider="ollama"` with an **empty**
`memory_extraction_endpoint`.

So the `ollama_url` → `{url}/api/chat` derivation in `from_settings` is the only reason the
default configuration is constructible at all. Remove it and the service fails to start on
its own defaults. `RUN-07` reads like a convenience feature; it is actually a dependency of
the default deployment, and it now says so in the plan.

The corrected test is also better than the one I meant to write. "A `direct_json`
deployment that forgets its endpoint cannot start" is a real operational property; "returns
an empty string" was a guess about an internal that I had no reason to care about.

## 50. Global state in a module needs an explicit reset in its tests

`_resolve_ollama_request_mode` caches the negotiated mode in a module-level dict keyed by
`(endpoint, model)`. That is correct for production — probing on every extraction would add
a round trip to every turn — but in a test suite it means whichever test runs first decides
the answer for every test after it, and the coupling is invisible until someone reorders
the file.

The `RUN-11` tests clear the cache in an autouse fixture, before and after. Before matters
as much as after: a test that assumes an empty cache but inherits a populated one fails for
a reason that has nothing to do with its own subject.

The cache also has a deliberate hole worth pinning: a probe that *fails* is not cached, so a
transient outage cannot pin the mode for the process lifetime. That is asserted by probing
twice and expecting two attempts — the only way to distinguish "not cached" from "cached
and never re-read".

## 51. Dropping my own duplicate rather than keeping it

I wrote a local `StubSemanticProvider` for the semantic recall tests because
`doubles.FakeEmbeddingProvider.health()` returned an always-truthy `ProviderHealth`. The
other session then fixed the double to return a plain `bool`, which made my stub redundant
and — worse — made its docstring false, since it explained itself by describing behaviour
that no longer existed.

Removed, and the tests now use the shared double. The rule I applied: a local duplicate is
justified by a difference in contract, and when the difference goes away so does the
justification. A stale explanation for why something exists is more expensive than the
duplication itself, because the next reader has to verify it before they can trust anything
around it.

---

## 30b. The SCH-14 defect has no preventer, but it does have a detector

*(Numbered 30b because the parallel session took 30–36; mine continue from here.)*

`DIA-15` is the most useful test in the diagnostics file, and it only works *because*
`SCH-14` is unfixed.

`SCH-14` is the finding that the unique index guaranteeing one active record per exclusive
slot stops firing at global scope — migration 0003 added the nullable `scope_project_id` to
it, and SQL unique indexes treat NULLs as distinct. The database will accept the second
row.

`inspect_memory_invariants` runs its own `GROUP BY subject_key, memory_type, domain_key,
slot_key HAVING count(*) > 1` and catches exactly that pair. So the invariant checker finds
what the constraint fails to prevent.

That distinction is worth stating plainly, because it changes how bad `SCH-14` is. Without
a detector it would be a silent corruption: two contradictory answers to "what is my name?"
with nothing able to tell you. With one, an operator can ask whether the gap has actually
cost anything — and when I checked the real profile databases earlier, it hadn't. The
finding stays serious, but it is *findable*, and the fix ordering I recorded (check for
duplicates first, then rebuild the index) has a tool to do the checking with.

The test is written so that repairing `SCH-14` turns it red: the inserts it depends on start
failing. That is the correct outcome, and the docstring says what to rewrite it into.

## 31b. Three more plan items that were wrong about the code

Same pattern as decisions 21 and 28 — my plan encoded assumptions from a module's shape
rather than its source. All three are pinned as-is with the reasoning:

- **`DIA-04`**: `snapshot()` returns only codes that have been recorded, not a zero-filled
  dict of every code. Defensible — an absent key means "never happened" while a present
  zero would mean "happened and counted zero", and for counters like *semantic hits dropped
  for wrong owner* that difference is real. The cost is that consumers must write
  `.get(code, 0)`, which is now written down rather than discovered.
- **`DIA-03`**: a metrics reader built for another owner does not return zeros — it raises.
  Every call re-checks the database's owner binding. That is stronger than what I assumed
  and better for a per-profile store, because a reader silently returning zeros against the
  wrong profile is indistinguishable from a healthy idle one.
- **`DIA-06`**: an unmigrated database fails with `OperationalError` (no binding table),
  not the `ValueError` raised when the table exists with the wrong number of rows. Two
  distinct causes, both refusing. I split it into two tests rather than assert one broad
  exception, so the migrated-but-unbound case is covered on its own.

## 32b. Tier 4 is closed except for orphan detection

Ten items remain open in Tier 4, and one is worth naming: `DIA-16` (orphan sources,
relations and derived rows). I marked it open rather than claiming it — the diagnostics
module does check for orphans, but constructing genuine orphans requires defeating the
foreign keys that exist to prevent them, and doing that convincingly needs the
`PRAGMA foreign_keys` manipulation that the schema tests already own. It belongs with
`SCH`, not here, and pretending otherwise would have inflated the count.

## 52. A docstring that promises what the function does not do

`drain_memory_outbox` says it "returns the number of completed targets and never raises
into the caller". It has no `try`/`except`. A failure from `lease_batch` or `process_batch`
propagates straight out.

The property is real; it is just implemented somewhere else.
`NeoChatService._build_memory_indexes` wraps the call, catches `Exception`, and logs
`memory_index_build_failed`. So the system behaves as documented — an indexing failure
never loses a memory that was stored correctly — and there is no user-visible bug here.

I pinned it as it is rather than as written, and tested both halves: the propagation in
`drain_memory_outbox`, and the handler at the call site. The call-site test reads the
source, which is a weak form of assertion, but the alternative was asserting nothing about
the only thing standing between a locked database and a lost memory.

This is the fifth instance of the pattern from decision 44, and the first where the
misplacement is *documented as if it were here*. The earlier four were silent — a plan item
assumed a guarantee and the layer simply did not implement it. This one actively tells the
next caller they are safe when they are not. That makes it worse than the others despite
being the least consequential today, because the cost arrives with the second caller and
looks nothing like a documentation problem when it does.

## 53. Tests must not create real profile directories

`database_url_for` calls `mkdir(parents=True, exist_ok=True)` on a path derived from the
user's data directory, and the non-guest key path reads the account registry. A naive
`build_memory_runtime` test would therefore leave real profile directories behind on the
developer's machine, which then need cleaning up by hand.

The fixture redirects `_root()` into `tmp_path` by patching `get_base_settings`, and every
test profile is a guest, which avoids the account registry entirely. A guest derives its key
material from an `owner_id` file in its own directory, so the fixture seeds that too — the
whole thing stays inside the test's temporary tree.

Worth stating as a rule rather than a fix: a test that touches profile creation must
redirect the root *before* the first call, because the directory is created as a side effect
of asking for the database URL, not by a separate setup step.

## 54. "Idempotent" and "cached" are different claims

`RUN-13` asks whether `_ensure_memory_schema` is idempotent across calls. Asserting that a
second call succeeds proves idempotence and nothing else — it would pass with the cache
deleted and the migration re-run every time, which is precisely the write contention the
function exists to remove. A runtime is built several times per chat turn, and each rebuild
would otherwise open a write-capable connection to the database the chat worker is writing
to.

So the assertion is on the number of engines built, not on the outcome. The same reasoning
applies to `_verified_memory_schemas` being cleared before *and* after each test: it is
process-lifetime state keyed by `(url, owner, identity)`, and a test asserting the migration
ran would otherwise pass or fail on ordering alone.

---

## 33b. The adapters are tested as an idempotency-namespace boundary

**What:** Most of the `ADP-*` tests are about which idempotency *surface* a write uses,
rather than about the write itself.

**Why:** The adapters are deliberately thin. Every one of them ends at the same mutation
coordinator, so testing "does create create" over and over would be testing the kernel
seven times. What each adapter uniquely decides is the write's identity: which actor, which
source, and which idempotency namespace.

That namespacing is load-bearing. There are seven surfaces — `chat`, `review`, `import`,
`agent`, `maintenance`, `manual`, `source_change` — and a collision between any two would
mean the second caller's write is silently swallowed as a replay of the first. The concrete
failure: a reviewer accepts a candidate while a background extraction worker writes the
same fact, and one of the two decisions disappears with no error anywhere. So there is a
test feeding every surface the *same* logical identifier and asserting all seven keys
differ.

The review key gets extra attention because it encodes a decision rather than a fact:
accepting and rejecting one candidate must hash differently (otherwise reject-then-accept
is a no-op), and a candidate edited between reviews must hash differently again (otherwise
the second reviewer's decision is lost to the first).

## 34b. Owner isolation turns out to be enforced twice, at different depths

Writing `SRC-05` I expected the cross-owner detach to fail at the command's owner check.
It fails earlier, in the migration binding check — `MemoryMigrationError:
memory_owner_database_binding_mismatch` — which refuses to open a database whose recorded
owner disagrees with the caller's.

Two independent layers stop it, and the outer one stops it before the connection is even
usable. I pinned the outer failure explicitly rather than catching a broad exception,
because *which* layer refuses is the interesting part: if someone later removes the binding
check believing the command check covers it, this test names what was lost.

Ruff caught me writing `pytest.raises(Exception)` here and in one other place. It was right
to: a blind exception assertion passes on any failure, including the test being broken. Both
are now specific, which is the same lesson the parallel session hit from a different angle —
an assertion that accepts more than one world isn't pinning anything.

## 35b. Three Tier 3 items left open rather than claimed

`SRC-01`, `SRC-02` and `SRC-03` need a memory that genuinely has persisted source rows, and
attaching one means going through the full extraction-and-persist path rather than inserting
a record directly. That is a fixture worth building, but it belongs with the mutation tests
that already own source persistence, not bolted onto the adapter file. Marked open rather
than approximated — a test that detaches a source which was never attached would pass while
proving nothing, which is the exact failure mode I have been auditing for elsewhere.

## 55. DELETE is not idempotent here, and that is the better answer

`API-15` expected a repeated `DELETE /memory/{id}` to succeed. It returns 404: the route
resolves the record through `_record_or_404` first, and a forgotten record is no longer
listed.

I pinned the behaviour rather than the plan, because the plan's version is worse for the
only caller that matters. This API is driven by a browser. Two tabs open on the same memory,
both showing a delete button: with an idempotent delete, the second tab reports success and
the user believes they deleted something that was already gone. With a 404 they learn the
record no longer exists. On a *missing* resource, a 404 is a defensible REST reading and the
more informative one.

The one thing that would change my mind is a retrying client — an automatic retry after a
dropped response would see 404 and report a failure for a delete that worked. Nothing here
retries, so it stays as it is, and it is now written down for whoever adds one.

## 56. Testing the HTTP layer without creating real profiles

`build_memory_runtime` resolves a profile database by calling `database_url_for`, which
creates the directory as a side effect. Left alone, these tests would scatter real profile
directories under the developer's data directory — the ones that then have to be found and
deleted by hand.

Every API test therefore redirects `_root()` into `tmp_path` before the first request and
uses guest profiles, which never open the account registry. The owner-isolation test creates
its *second* profile the same way, so even the cross-owner case stays inside the temporary
tree.

This also happens to make the isolation test honest. The foreign profile gets its own
database, so the 404 is produced by the profile actually being empty — the real mechanism —
rather than by a filter I would otherwise have had to fake.

## 57. Two more disjunction assertions, caught before commit this time

`assert 400 <= status < 500` for prohibited content, and `assert status in {404, 503}` for a
foreign id. Both would have passed on outcomes I did not intend: the first on any client
error including a validation failure that never reached the content guard, the second
whether the record was properly hidden *or* the entire runtime failed to build — which are
opposite outcomes wearing the same test.

Replaced with the exact values, which I got by running the requests and reading them: 409
with `rejection_code: prohibited_sensitive_content`, and 404 with the same body an unknown
id returns. The second assertion is now stronger than a status check alone — it compares the
foreign-id response to the unknown-id response and requires them to be identical, which is
the actual security property. A different body for "exists but not yours" would confirm the
record's existence to someone who cannot read it.

That makes six of these across my slice. The tell is always the same: an assertion that
accepts a range or a set is an assertion I have not finished writing.

---

## 36b. The deferred SRC tests, and the vacuous one they replaced

Decision 35b left `SRC-01/02/03` open because they needed genuinely persisted source rows
and I would not approximate them. They are done now, and building the fixture properly
exposed that one of the tests I *had* written was vacuous.

`SRC-04` ("detaching never runs a lifecycle command") passed a randomly generated
`source_id`. That returns `SOURCE_NOT_FOUND`, so nothing was detached, so "the record is
still active" was trivially true — the test would have passed against an implementation
that deleted the memory on every real detach. Exactly the failure the parallel session kept
finding in its own work, and the fifth instance between us.

The replacement creates a memory with real evidence, reads the actual source id out of the
database, detaches it, and asserts three things at once: the source row is now inactive, the
memory is **still active**, and the outcome is `NEEDS_REVIEW`. That last part is the design
worth pinning — editing away the message you learned something from is not a request to
forget it, so a memory whose last support disappears is surfaced for review rather than
silently kept or silently deleted.

The "unknown source id" case is now its own named test, so the real ones cannot quietly
collapse back into it.

**One fixture detail worth recording**, because it cost me a confusing empty result: the
`engine` fixture and the mutation coordinator use *different database files*. The
coordinator builds its own engine from `execution_context.database_url` (`profile.db`),
while `engine` is a separately migrated `memory.db`. Tests that go through an adapter and
then query rows directly must query the coordinator's database, not the fixture's.

## 37b. `detach_source` refuses cross-owner access two ways, and the difference is deliberate

Writing `SRC-05b` I found the two paths behave differently, and both are right:

- A **command** whose owner disagrees with the context returns `SourceChangeOutcome.
  OWNER_MISMATCH` as a *result*. That is a caller error, and the caller should see it in the
  value they get back.
- A **context** pointing at a database bound to someone else *raises*, from the migration
  binding check, before the connection is usable. That is an environment fault, and it
  should stop everything.

Both now have tests, and the docstrings say which is which. Worth the distinction because
"cross-owner access is refused" would have been satisfied by testing either one, while
missing that the system draws a line between a bad request and a bad deployment.

## 58. A flag that nothing can set, tested anyway

`health_routes_enabled` gates all three maintenance routes. `MemorySettings.from_settings`
sets it to a literal `True` — it is not derived from any setting, so no deployment can
currently switch it off. The only way to exercise the guard is to substitute the flags
object.

I tested it regardless, and the reason is worth stating: the guard is real code on a
security-relevant path, and the *reason* it cannot be reached today is a missing wire-up
rather than a decision to remove it. If someone later adds `NEO_MEMORY_HEALTH_ROUTES=false`
to the settings, they will wire it to this flag and expect the guard to work. A test that
exists now means that day is a one-line change rather than a one-line change plus an
unverified assumption.

The behaviour is also right in a way worth pinning: it returns **404**, not 403. A disabled
administrative control should be indistinguishable from one that does not exist, because a
403 confirms the route is there and worth attacking.

Recorded rather than "fixed" — wiring the flag to a setting is a source change and outside
what this suite does.

## 59. `HLT-06` describes a token that never crosses the wire

The plan asks for validation of "the owner token" against `_UUID_TOKEN_PATTERN`. No request
to these routes carries an owner: the owner comes from the session profile, and the database
binding is re-derived from the migration ledger. `_UUID_TOKEN_PATTERN` exists only as a
building block of the reconciliation checkpoint grammar.

So the pattern is tested through the checkpoint shapes it composes, which is where a
malformed value could actually arrive. That includes a SQL-shaped checkpoint, deliberately:
the checkpoint is the one free-text field on this surface and it ends up in a cursor, so
"rejected by the contract before it becomes a query" is the property worth having.

## 60. Route ordering is load-bearing and now has a test

`GET /api/memory/health` and `GET /api/memory/{memory_id}` both match the same path. The
health router is included before the memory router in `create_app`, and that ordering is the
only reason `health` is not parsed as a memory id — reversed, the route would start
returning a 422 for an invalid UUID.

Nothing about that is visible from either router file, and both look independently correct.
There is now a test that fails if the `include_router` order changes, which is the only
place the coupling can be caught.

## 61. Two more weak assertions, same tell as the other six

`assert "targets" in body or "owner_id" in body or body` — the third clause is truthy for
any non-empty dict, so the first two never mattered. Replaced with the actual coverage
fields and their expected zeroes, which is the thing an operator reads this route for.

`assert response.json()["detail"] in {three possible codes}` — a set, in a test already
parametrised over the three routes. The expected code is now part of the parametrisation, so
each route asserts exactly its own.

Eight now. The rule has stabilised into something I can apply without thinking: **if an
assertion accepts more than one outcome, either the parametrisation is missing a column or
I have not yet found out what the code does.** Both times here it was the first.

---

## 38b. The eleventh finding, and why writing one test found it

`EXC-19` asked for "a retraction resolving to many targets forgets all of them". I had
deferred it once as needing an awkward setup, then realised two memories can hold one value
legitimately — same display text, different domains — with no need to exploit `SCH-14` at
all.

Setting that up and running it produced: two `FORGET` decisions, one `forgotten`, one
`failed`, one memory still active, and the turn reporting `APPLIED`.

The give-away was that **both decisions carried the same `operation_id`**. That is not
something a test asserts by default; I only looked because "failed" with no obvious cause
demanded an explanation. `_apply_retraction` builds its idempotency key from the retraction
and is called once per target, so every target in the loop computes an identical key. The
second call is treated as a replay of the first, matches an idempotency record naming a
different memory, and fails.

What makes this worth the emphasis: the comment directly above the loop says it exists
because "retracting only the first left the duplicates active and the value returned on the
next recall." Somebody already found this symptom and fixed half of it — the loop was
added, the key was not made per-target. The test that would have caught the incomplete fix
is precisely the one the plan listed and I had put off.

Recorded as a strict `xfail` with a passing companion pinning the current behaviour, per
decision 14. The fix is one line: include the target memory id in the key.

## 39b. A branch that is only reachable with foreign keys off

`OBX-11` handles an upsert whose canonical record has vanished. I tried to set that up by
deleting the record and the foreign key refused — correctly, since the outbox event points
at it.

So the branch is unreachable in a database with `PRAGMA foreign_keys=ON`. That does not make
it dead code, and the test does not skip: **SQLite enforces foreign keys only when that
pragma is set, per connection, and off is the default.** `app/db/session.py` sets it, so the
running application is protected — but any other process opening the same file is not: a
migration script, `sqlite3` at a prompt, a future worker that builds its own engine.

The test therefore switches the pragma off for the deletion, which recreates exactly the
state the branch defends against, and the docstring says why rather than leaving it looking
like a workaround. This is the same reasoning as `conftest.py` turning foreign keys *on* for
the test engine: SQLite's default is the unusual one, and both directions need stating.

## 62. The schema makes the sensitive-candidate masking redundant, which is the point

`list_candidates` renders `"[sensitive memory]"` instead of the stored text when a candidate
is sensitive. I set out to test that by seeding a sensitive candidate with a real
`display_text`, and the database refused the insert:
`CHECK constraint failed: ck_memory_candidates_payload_shape`.

The constraint requires a sensitive candidate to have `canonical_payload` and `display_text`
**NULL**, with the encrypted columns populated. So the plaintext the route is "hiding" cannot
exist on that row at all. The substitution is masking an absence rather than withholding a
value it could have shown.

That is two independent layers — the schema refusing to store it, and the route refusing to
render it — and neither depends on the other being correct. The test now seeds the encrypted
shape, which is the only shape that exists in production anyway, and asserts the ciphertext
does not appear in the response either.

Worth recording because I would not have found the stronger guarantee by reading the route.
The database told me by rejecting a row I thought was reasonable.

## 63. A test suite that quietly depended on a model server running

The full suite went from 72 seconds to still-running at ten minutes. The process was at 0%
CPU, which rules out slow code and points at waiting on something external.

`build_memory_runtime` calls `_resolve_ollama_request_mode` whenever extraction is enabled,
and that probes a real Ollama endpoint with a warmup timeout of up to **300 seconds**. My API
tests build a runtime on nearly every request and never disabled extraction, so the suite's
runtime — and, on a machine with no model server, whether it finished at all — depended on
something entirely outside the tests.

Two details made it worse than it looks. A *failed* probe is deliberately not cached
(decision 50 covers why: a transient outage must not pin the mode for the process lifetime),
so every runtime build retried it. And it passed locally at first, because a probe against a
running Ollama returns quickly — the dependency was invisible exactly when it was harmless.

Fixed by disabling live extraction in the API fixtures. The health-route tests do not build a
runtime, so they never had the problem.

The general rule I should have applied from the start: a unit or integration test that
constructs production wiring inherits every network call that wiring makes. "It passes on my
machine in 40 seconds" is not evidence there is no network call — it is evidence the call
succeeded.

---

## 40b. Tier 4 is closed; two notes on how the last two items were reached

**`OBX-26`** covers the `memory_derived_state` rows that health and coverage read from.
Nothing else records "is this memory indexed, and by which model", so a row that drifts out
of step with the delivery makes a broken index look healthy — the one thing health
reporting must never do. Three cases: a success records `current` plus the provider
identity, a failure records `failed` plus the error code *without touching the healthy
target*, and a later success **clears** the earlier error. That last one matters because a
stale `last_error_code` on a now-healthy row keeps an alert firing after the cause is fixed,
which is how people learn to ignore alerts.

**`DIA-16`** needed the same foreign-keys-off technique as `OBX-11`, and for the same
reason — with the pragma on, a source row's key makes its record undeletable, so the orphan
state is unreachable. The invariant checker exists precisely to find damage the constraints
were supposed to prevent, and it is only trustworthy if it has been shown to fire. The test
asserts the store is clean *first*, so a check that reported violations unconditionally
would fail rather than pass.

I stopped hand-writing the INSERT after guessing two column names wrong and switched to the
ORM model, which fills its own defaults. Faster, and it cannot drift from the schema.

## 41b. Where the eleven findings stand

All eleven remain recorded as strict `xfail`s rather than fixed, per the decision in §14
and your explicit call on `SCH-14`. Two of them are worth re-reading together, because they
compound:

- **`SCH-14`** lets two active records occupy one exclusive slot.
- **`EXC-19c`** means a "forget" matching several memories removes only the first, and
  reports success.

The first makes duplicates more likely; the second makes them harder to remove and lies
about having done so. Either alone is a bug; together they describe a store that can
accumulate contradictory facts the user cannot fully delete. `DIA-15` is the mitigation —
`inspect_memory_invariants` detects the duplicate state — so the sequence for anyone picking
this up is: run the invariant check, fix `EXC-19c` (one line: put the target memory id in
the idempotency key), then fix `SCH-14`'s index, in that order. Fixing the index first fails
against data that already violates it.

---

## 42b. Concurrency is tested with real threads, and one assertion is a range

**What:** The `CNC-*` tests spawn real threads against a real SQLite file rather than
simulating a race.

**Why:** SQLite serialises writes, but serialisation only means two writers do not
interleave — it says nothing about whether the second does the right thing on finding the
world changed. `BEGIN IMMEDIATE`, the busy-timeout retry, and the optimistic revision check
all behave differently under genuine contention than under a scripted one, and a simulated
race would only test the code path I *expected* to be taken.

**`CNC-01` asserts a range, deliberately.** Four writers race to put different values in
one globally-scoped exclusive slot — `identity:global:name`, which is exactly the case
`SCH-14` says the unique index no longer protects. Asserting "exactly one record survives"
would encode the *fix*, so the test would fail today and read as a regression. Asserting
"exactly four" would encode the bug as intended behaviour, so it would fail when someone
fixes it. Neither is honest. What is true regardless, and worth guarding, is that nothing
is corrupted: every writer either succeeds or fails cleanly, and the store ends with between
one and four active records. When `SCH-14` is fixed the range can tighten to one, and the
docstring says so.

## 43b. Two performance tripwires that started out proving nothing

Both `PRF` tests over recall passed immediately, and both were worthless as first written.
I found this by printing what they actually measured rather than trusting the green.

**`PRF-05`** (the recall context stays under `MAX_RECALL_CONTEXT_CHARS` = 2,400) returned
five items totalling **164 characters**. It would have passed against a build with no
character bound at all, because two *other* limits bind first: identical slot keys collapse
records to one, and the record-count limit stops at five long before 2,400 characters. The
fix was to give every record its own slot and ~1,000 characters of text, so twenty
candidates compete and only two fit. There is now also an assertion that one *more* record
would have overshot — otherwise the test still could not distinguish "the bound worked" from
"there was not much to return".

**`PRF-02`** (recall does not query per record) was written with a threshold of fifty
statements. The real number is four, over two hundred records. Fifty would have caught an
N+1 — but so would fifteen, with far more margin for noticing a smaller regression, so the
bound is now fifteen and the comment records the observed four.

The general point, and the reason this gets its own section: **a passing performance test is
the easiest kind to leave broken**, because green looks like the goal. The only way to know
a bound is doing work is to make the thing it bounds actually press against it.

## 44b. Verifying the suite makes no network calls

The parallel session found the whole suite had been silently depending on a running Ollama —
a runtime fixture probed a live endpoint with a 300-second warmup timeout, and it looked
fine locally because a running service answers fast.

I checked my own files by monkeypatching `socket.socket.connect` to raise and running all
seven: 404 passed, 3 xfailed, **zero connect attempts**. Worth doing that way rather than by
reading the code, because `test_extraction_providers.py` constructs three real
`StdlibJsonHttpTransport` objects — construction never connects, but I would not want to
assert that from inspection.

I proposed making it permanent as an autouse fixture in `tests/memory/conftest.py`, with an
opt-out marker for any test that genuinely needs a socket. Not added unilaterally: it lands
in a shared file and would break the other session's API tests if any of them need one.
Third time today a problem was invisible because the happy path was fast; a socket guard is
a second source that costs nothing per run.

## 64. Two of my tests were green on the error path, and only a socket guard showed it

The other session proposed an autouse fixture that makes any real `socket.connect` fail, and
asked whether it would break my files. I ran it rather than reasoning about it, which was the
right call: **two tests in `test_api_memory_health.py` were connecting to `127.0.0.1:11434`.**

They passed anyway, and that is the part worth writing down. `_maintenance_for_profile`
builds a real `OllamaEmbeddingProvider` and hands `provider.health` to maintenance, which
calls it during `coverage()`. With no Ollama running, the provider catches the connection
error and reports unhealthy; `coverage()` still returns its counts, and my assertions still
held. So the tests were green *via the failure path*, and would have been green either way —
the only observable difference was a network round trip and a few hundred milliseconds.

Stubbing the provider fixed both problems at once. The tests no longer touch the network, and
they now exercise the path they were written for: a healthy provider reporting real coverage,
rather than an unhealthy one being tolerated.

Building the stub surfaced a second thing worth knowing: `ValidatedMemoryEmbeddingProvider`
refuses to construct unless the wrapped provider exposes non-empty `provider_name` and
`model_name`. My first stub omitted `provider_name` and every health route turned 503. The
identity of an embedding provider is not decoration — it is what the vector index stamps into
each row to decide whether a stored vector is still comparable (RCL-48b).

**The general point.** "This test passes" answers a narrower question than it appears to.
It does not say the code took the path the test describes. Three times today a problem hid
behind a fast, self-consistent happy path — an item count that agreed with itself, a progress
sentence that agreed with its own table, and now a network call that succeeded. A socket
guard is a cheap second source: it cannot tell you a test is meaningful, but it can tell you
the test reached outside the process, which is something no assertion in the test itself will
ever mention.

---

## 45b. The suite now proves it makes no network calls

`tests/memory/conftest.py` has an autouse `block_network` fixture. Every external
collaborator in this layer has a double, so a socket here is always a mistake — but a quiet
one, and both sessions working on this suite hit it independently.

**Why it records attempts rather than only raising.** Raising alone would have caught the
first case (a runtime fixture probing a live Ollama endpoint with a 300-second warmup
timeout) but not the second: two health tests called a real embedding provider, the provider
*caught* the connection error and reported unhealthy, and the assertions still held. Those
tests were green **via the failure path** — they would have passed with or without a model
server, and the only observable difference was a round trip. So the fixture records each
attempt and fails at teardown even when the test body passed. That is what turns "something
connects" into "these two tests, by name".

Both patterns are covered: a connection that raises fails immediately, and one that is
swallowed fails at teardown. `connect_ex` is patched as well as `connect`, since it returns
an error code rather than raising and would otherwise walk straight past a guard on
`connect` alone.

**The opt-out is a marker, not a config flag.** `@pytest.mark.allow_network` puts the
exception in the test's own source, where a reviewer sees it, rather than in a settings file
where nobody does.

**Why this was worth the trouble.** On a machine with Ollama running, the dependency was
invisible: the suite took 72 seconds and passed. Without it, 0% CPU and still running at ten
minutes. The dependency was undetectable exactly while it was harmless, which is the same
shape as the other two things caught today — an item count that was internally consistent
and wrong, and a progress summary that was coherent and wrong. None had a second source. A
socket guard is a second source that costs nothing per run.

## 65. Two defects in the older retrieval subsystem, both found by doing the obvious thing

**Twelfth: retrieval is not scope-isolated.** `MemoryRetriever.retrieve` has two guards
meant to exclude other scopes, and both read:

```python
if request.scope_type and item["scope_type"] != request.scope_type
   and item["scope_id"] != request.scope_id:
    continue
```

`and` where isolation needs `or`. An item is skipped only when its scope *type* and its
scope *id* both differ — so any two scopes sharing a type see each other's items. Every pair
of chats shares the type `chat`. A query in one conversation returns another conversation's
stored text.

What makes it easy to miss is the scorer: same-scope items get a +0.22 bonus, so the correct
result still ranks first and the leak only appears further down the list. The test therefore
asserts both — that the foreign item is present, *and* that the local one is on top — because
asserting only the second would have looked like a passing isolation test.

This subsystem stores content a user pasted into a chat, so this is an information boundary,
not a ranking preference. Recorded as a strict `xfail` expressing the isolating behaviour.

**Thirteenth: renaming an item returns 500.** `store.update_item` merges the patch and hands
it to `upsert_item`, which decides insert-versus-update by looking for a row matching
`(scope_type, scope_id, source_type, source_id, memory_type, title)`. When the patch changes
the title, that lookup matches nothing, so the code takes the INSERT branch — carrying the
item's existing id — and hits `UNIQUE constraint failed` on the primary key.

Every other field patches fine, which is why it survived: the failure needs the one edit a
user is most likely to make to a saved note. `update_item` knows the id it is updating; it
should not be re-deriving identity from content.

Both are in a subsystem the plan itself calls "older" and asks only for a working-order pass.
That framing turned out to be the reason to look, not a reason to look less carefully.

## 66. `/index` accepts a payload it ignores

`RTV-01` reads "indexes an item and is idempotent", so I posted an item. It returned **200**
and indexed nothing.

`POST /workspace-memory/index` does not accept an item. `MemoryIndexRequest` carries a scope
and a list of source types, and the endpoint sweeps *existing* rows — context summaries,
agentic runs — into the retrieval store. The model does not forbid extra fields, so a title
and content are accepted, discarded, and reported as success.

Pinned as a sweep instead, with a re-sweep asserting no duplication. The wider point is the
one that cost me the time: a 200 means the request was understood, not that it did what the
caller meant. An endpoint whose request model ignores unknown fields will confirm any
misunderstanding you bring to it.

## 67. These subsystems write to the application database, not a profile

`memory_retrieval` and `context_memory` both resolve their SQLite path from
`get_settings().database_url` at call time — the application database, which in a normal
checkout is the `neo_memory.db` sitting in the repository root.

So a test that exercised these routers without redirecting that setting would write its rows
into the developer's real database. Both store modules are patched, not the settings cache,
because each reads it independently.

Same category as the profile-directory care in decision 53, and worth stating as one rule:
before testing any subsystem, find out which file it writes to. Two of the three memory
subsystems in this application answer that question differently from the one I had just
finished testing.

## 68. A whole-database sweep is a different test from a per-component check

Individual layers already had privacy tests: SCH-12 pins the encrypted column shape, MUT-13
checks one operation row, IDX-01 stops a sensitive record reaching the index. All of them
ask "did *this* component behave?"

The PRV tests ask a question none of those can: after the whole pipeline has run, is the
string anywhere at all? Each one enumerates `MEMORY_TABLES` and names the table it found the
value in.

The difference matters for a specific failure. A per-component test keeps passing when
someone adds a table — a new audit log, a new cache — that starts holding the same content.
Nothing in the existing suite would notice, because every existing test is scoped to a
component that did not change. A sweep driven off the migration's own table list notices
automatically.

**Every sweep needed a positive control**, and writing them was not optional politeness.
`_assert_absent` passes trivially if the value was never stored, if the tables are empty, or
if `_sweep` returns nothing. So there is a test asserting the sweep covers every managed
table, and a test asserting ordinary content *is* found by it. The erase test runs its
control inline: it asserts the value is present, then erases, then asserts it is gone.
Without that, "erase leaves no trace" would pass against a create that silently failed.

## 69. Testing isolation at the enumeration rather than the operation

`ISO-07` asks that maintenance never touch a foreign owner. My first version called
`rebuild_owner()` and asserted the foreign record was not in the index afterwards. The
positive control failed: nothing was indexed at all, because the rebuild has its own
eligibility rules and my fixture records did not meet them under a wall-clock `now`.

That failure is the finding. Had I written only the negative assertion, it would have passed
— reporting isolation while proving nothing except that the rebuild did no work. It is the
same shape as decision 48: I was not testing the filter, I was testing whichever path
happened to win.

The fix was to assert at `repository.list_index_candidates`, the owner-scoped enumeration
`rebuild_owner` reads through. That is where the guarantee actually lives — it filters in
SQL — and the positive control is meaningful there because the enumeration returns the
owner's own record unconditionally.

The general rule: when an operation has preconditions of its own, testing a *property* of
that operation risks the preconditions silently making the test vacuous. Test the mechanism
the property comes from instead.

## 70. Where owner isolation actually comes from, stated once

Two facts live in different files and neither one alone answers the question.

`NRM-15` pins that a NORMAL fingerprint ignores the owner: two profiles storing the same
ordinary fact produce the *same* digest. `CRY-06` pins that a keyed (SENSITIVE) fingerprint
differs across owners. Read separately, the first looks like a leak.

It is not. For normal records isolation comes from the `owner_id` column and the
owner-scoped unique index (`SCH-16`), not from the digest. For sensitive records the digest
is owner-bound as well, because there the fingerprint would otherwise be a stable identifier
that one profile could use to test whether another had stored a particular fact.

Both are now asserted side by side in the isolation tests, with the reasoning attached.
That pairing is the whole argument for a cross-cutting tier: the individual facts were
already covered, and the thing that was missing was the sentence explaining which of them
carries the guarantee.

## 71. The correction journey does not do what E2E-03 says, and the code is right

`E2E-03` asks that "Actually, now I want to improve at watercolour" replace the stored goal.
It does not. The turn resolves to `unlinked_exclusive_slot_conflict` and goes to review with
the old goal untouched.

The reason is `ground_retraction`. The retraction's `old_value_hint` is "improve at urban
sketching", and that text appears nowhere in the correction message — the user implied the
old goal was finished without naming it. An ungrounded retraction is refused, so the
candidate reaches the resolver with no `old_value_hints`, falls through to the exclusive-slot
occupancy check, and is routed to review.

That is the right outcome. The alternative is deleting a goal on the strength of an
inference, which is the failure mode the whole grounding layer exists to prevent. Review is
where an ambiguous delete belongs.

The journey the plan *meant* also works, and is now pinned separately: "I no longer want to
improve at urban sketching. I want to improve at watercolour." names both halves, the
preparser handles it deterministically with both grounded, and the replacement happens. Two
tests, because the difference between them is the entire safety property.

## 72. Two journeys that were green for the wrong reason

**A scripted span must cite the message it arrived on.** `doubles.assertion` defaults to
`message_id="m1"`. My multi-turn journeys used `m2` for the second turn while their spans
still claimed `m1`, so every second turn was rejected as
`source_message_not_user_authorized`. E2E-02 asserts "restating creates no second record" and
passed — because the restatement never got stored at all. A rejection is indistinguishable
from correct de-duplication when you only look at the record count. Both turns are now
asserted to have been *applied* before the count is checked.

**The domain filter test named a domain that does not exist.** I filtered on `"art"`, the
hint I had scripted, and got an empty result. But `_domain_for` could not ground "art" in the
message and fell back to `global` (decision 35), so the record's domain was never "art" —
the empty result came from filtering on a domain nothing was stored under. It now asserts
both directions: the record's own domain returns it, another domain does not.

Both are the same mistake in different clothes: a negative assertion that passes because the
setup failed rather than because the filter worked. Every one of these I have found came
from asking, after green, *which* of the ways this could pass actually happened.

## 73. A filename collision destroyed uncommitted work

Both sessions independently created `tests/memory/test_e2e_journeys.py`. `Write` overwrites,
so the second write replaced the first wholesale. Neither copy was committed, so there was no
reflog entry and nothing to recover — the work was simply gone.

Every convention we had built protects the *index*: stage explicit paths, re-read before
editing, regenerate the table rather than hand-editing it. None of them apply to a file that
does not exist yet, because there is nothing to re-read and no conflict to detect. Two agents
told to "write the E2E tests" reach for the same obvious name.

The coordination that failed was mine as much as the tooling's: I claimed *plan items*
(E2E-01..09), and plan items do not name files. The convention adopted afterwards is to claim
the filename. The other session added the sharper detail: `Write` reports "created" versus
"updated", and "updated" on a file you believe is new is a stop signal.

Recorded because the cost was real and the lesson is not about git. In a shared working
directory, "I am creating this file" is an assumption, and it is checkable before the write
rather than after.

---

## 46b. End-to-end journeys assert what a user would notice

**What:** The `E2E-*` tests walk a whole path — a turn arrives, extraction runs, a record
lands, recall finds it, a forget removes it — and assert on the store rather than on what a
function returned.

**Why they were written last.** A journey crossing five layers is only diagnostic when the
layers beneath it are pinned. Written first, a red E2E means opening four investigations at
once; written after everything else, a failure points at the seam between layers rather than
at any one of them. Both sessions agreed to hold them until the end for this reason, and it
was the right call.

Two of them are worth singling out for what they assert rather than what they cover:

- **`E2E-10`** sweeps *every table* in the profile database for the plaintext of a sensitive
  value, rather than checking the one column it is supposed to be encrypted in. The value
  passes through the candidate, the operation's command payload and the source excerpt on
  its way to the record, and any of those could hold it in the clear. Checking the intended
  column proves the intended column works; sweeping proves the promise.
- **`E2E-15`** makes the claim only a journey can. `test_mutations.py` already covers
  rollback stage by stage, so the addition here is that after a write is killed
  mid-transaction, **the next write still succeeds and is recallable**. A rollback that also
  poisoned the idempotency ledger would satisfy every "nothing was written" assertion while
  leaving the profile permanently unusable — which is the failure a user would actually
  experience.

## 47b. Two more tests of mine that passed while proving nothing

Both were caught by the parallel session's warnings rather than by me, and both are the same
shape as the five found before.

**`CNC-05`** built maintenance on the `engine` fixture (`memory.db`) while the concurrent
writes went through the mutation coordinator, which builds its own engine from
`database_url` (`profile.db`). Two different files. There was no contention at all, and the
test would have passed against a completely unsynchronised implementation. Both halves now
share one database, and there is a setup assertion that says so — because the next person to
touch this will not know those are different files either.

**`MNT-15`** created *only* a foreign record, rebuilt, and asserted the foreign record
survived. That passes just as well if the rebuild indexed nothing, and `rebuild_owner`
defaults to wall-clock `now` while the fixtures write at `FROZEN_NOW` — so "indexed nothing"
was the likely outcome rather than a hypothetical. It now proves the rebuild ran against
this owner's records first.

**The running tally is seven tests between the two sessions that were green while asserting
nothing.** Every one was found the same way: by asking, *after* it passed, which of the ways
it could pass actually happened. That question is the only reliable tool here, and it costs
one probe. The general form, sharpest as the other session put it: *testing that a filter
drops something is only meaningful once every other route to the thing is closed off first.*

## 48b. I overwrote another session's file, and the signal was already there

Worth recording as an incident rather than a lesson in the abstract, because it cost real
work.

Both sessions independently created `tests/memory/test_e2e_journeys.py`. `Write` overwrites,
so mine replaced theirs wholesale. Neither was committed, so there was nothing to recover.

**The tool told me and I did not read it.** `Write` reports "File **created** successfully"
for a new path and "The file ... has been **updated** successfully" for an existing one. I
got "created" for `test_concurrency.py` minutes earlier and "updated" for this one. The
difference was in the output.

The convention we added afterwards — claim filenames, not just plan-item ranges — is worth
having, because two agents told to write "the E2E tests" both reach for the same name and a
message naming *items* does not prevent that. But the convention needs both parties to
remember it every time, whereas the tool result only needed one party to read it once. The
durable fix is the smaller one: **in a shared worktree, "I am creating this file" is an
assumption, not a fact**, and the Write result is where that assumption gets checked.

Related, and the reason it did not compound: when a stray `test_zz_probe.py` appeared that
I could not prove was mine, I left it rather than tidying it up. Deleting a file of
uncertain ownership is the same error one step further along.

---

## 49b. Is the memory layer ready for actual usage? A straight answer

The original goal was not a test count, it was readiness. So, plainly:

**The canonical memory layer is ready, with three defects that should be fixed first.** The
paths a user touches daily — storing a fact, recalling it, forgetting it, keeping a
sensitive value private, running with no local model installed — are covered end to end and
behave correctly. 2,103 tests pass across every tier, and every plan item is covered.

**Fix these three before daily reliance, in this order:**

1. **`EXC-19c`** — a "forget" matching several memories removes only the first and reports
   success. One line: put the target memory id in the idempotency key. First because it
   fails a deletion the user explicitly asked for *and says it worked*, and because the
   duplicates it leaves behind block the next fix.
2. **`SCH-14`** — the exclusive-slot uniqueness index stops firing at global scope, so two
   contradictory answers to "what is my name?" can coexist. Second because rebuilding a
   unique index fails against data that already violates it, so the duplicates must be
   cleared first. `inspect_memory_invariants` (`DIA-15`) finds them.
3. **`RTV-12`** — workspace retrieval leaks items between chats. Independent of the other
   two; fix whenever. `and` should be `or`.

**The other ten findings are real but not blocking.** Three are user-visible: the stemmer
missing `-es` plurals, so "sketches" does not match "sketching" (`RCL-21d`); "call me X"
depending on the local model rather than the deterministic path (`PRE-01b`); and renaming a
workspace item returning 500 (`RTV-09`). The remaining seven are narrow: a negation guard
missing one phrasing (`NRM-30b`), an address pattern missing some street suffixes
(`POL-15e`), a blank-check that only strips spaces (`SCH-11b`), a config validator that
misses exactly one value (`EXT-21d`), an unreachable failure code (`OBX-15`), a command that
cannot round-trip through JSON (`CON-21b`), and a declared constant contradicted by the
scorer (`RCL-31b`).

**What is *not* covered, stated so it is not mistaken for coverage:**

- The **application outside the memory layer** — 56,000 lines, 386 API operations, 20-odd
  feature services — has no tests. `tests/APP_TEST_PLAN.md` surveys and risk-ranks it; the
  deferred P0 band (profile isolation, the shell sandbox, patch application, workspace
  paths, credentials) is where I would go next.
- **Live model behaviour.** Every test scripts the model. That is deliberate — the suite
  runs in 71 seconds with no Ollama and no network, and a socket guard now proves it — but
  it means extraction quality is unverified. What is verified is that bad model output
  cannot corrupt the store.
- The **workspace retrieval and context-memory subsystems** got a working-order pass, not
  the treatment the canonical layer got: 57 tests against roughly 1,800 lines, and two
  defects surfaced in the first hour of looking. That ratio suggests there is more there.
- **Nothing.** Every one of the 880 plan items is now covered: 875 done, 5 partial, 0 open.
  The five partials are items whose desired behaviour is recorded as a strict `xfail`
  because the code does not yet do it.

**The most useful thing this exercise produced** is not the 2,100 tests. It is the thirteen
recorded defects — none of which was tracked anywhere beforehand, and one of which,
`EXC-19c`, sits directly beneath a source comment describing the very symptom it still
exhibits. Someone diagnosed that one correctly, wrote the loop to fix it, left the
idempotency key per-message instead of per-target, and moved on. A half-fix carrying a
comment that asserts it is a whole one.

And the lesson underneath, which cost more to learn than any single defect: **ten tests
across two sessions were green while asserting nothing.** Every one was caught the same way
— by asking, after it passed, *which of the ways this could pass actually happened.* The
sharpest form of it: a test whose assertion is a count asserts that the number you expected
happened, not that the thing you expected happened.

*Three numbers in this section were themselves stale within an hour of being written — the
findings table grew and the prose did not. They were caught by the other session reading it
against the table rather than trusting it. That is the same failure this document spends
several sections describing, appearing in the document about it, which is worth leaving on
the record rather than quietly correcting.*

---

## 50b. The defects are fixed, and the migration has been applied to your profiles

Everything in my column is fixed, each landing with its strict `xfail` removed in the same
commit — so a fix that did not work would fail the suite rather than pass quietly.

| Fix | What changed |
|---|---|
| **`SCH-14`** | Migration `0004` rebuilds the exclusive-slot unique index over `COALESCE(scope_project_id, '')`, so globally-scoped rows compare equal instead of every NULL being distinct. |
| **`SCH-11b`** | `trim()` with one argument strips spaces only. A `_not_blank` helper now uses the two-argument form naming every whitespace character, applied at all four sites. |
| **`EXT-21d`** | `x or y or 120` treated an explicit `0` as "not supplied". Resolved with `is None`. |
| **`OBX-15`** | The `except` handler called `_finish_target` a second time, which re-raised. It now tolerates a lost lease and reports `LEASE_LOST`. |
| **`CON-21b`** | `MemoryUpdatePatch` serialises only the fields that were set, so its own dump re-parses. An explicit null still serialises. |
| **`RCL-31b`** | The scorer now reads `USAGE_AFFECTS_RANKING`, and folds the freed weight into the lexical term so the scale is unchanged. |

**The `SCH-14` migration ran against your real profile databases.** Sequence, in order:

1. **Read-only inspection first.** Profile `1515a663…` was at revision 0003 with 3 active
   records; profile `30c07278…` was still at 0002 with none. **Neither held any duplicate
   exclusive slots**, so there was nothing to clean up before the index could be added —
   which is what made this safe to run at all.
2. **Backed both up** with `create_sqlite_backup`, each verified by `PRAGMA integrity_check`
   and a SHA-256 recorded, into `profiles/backups/pre-0004-<timestamp>/`.
3. **Applied the migration.** Both reached `0004`; `30c07278…` came through `0003` on the
   way. `inspect_memory_invariants` reports healthy with no violations on both.
4. **Verified afterwards, read-only.** The index now carries `COALESCE`, and all three of
   your memories are intact.

## 51b. Two fixes changed what other tests could set up, which is the point of strict xfails

`DIA-15` and `DIA-19` tested the invariant checker by *creating* two active records in one
exclusive slot — which only worked because `SCH-14` let them. Fixing the index turned both
red, exactly as `DIA-15`'s own docstring predicted it would, and asked for.

They now drop the index to reach that state deliberately, which is not a workaround: it is
the state a **pre-0004 database is genuinely in**, and the 0004 migration refuses to run
until those rows are cleared. The checker is the tool that finds them, so it has to be shown
working against precisely that data. `DIA-15b` adds the half that did not exist before — with
the index in place, the second insert is refused outright.

That is the whole argument for strict `xfail` over a skipped test or a comment: the suite
told me, at the moment the fix landed, which other tests had been quietly depending on the
bug.

## 52b. One near-miss worth recording

While checking whether some lint errors were pre-existing, I ran `git stash` — in a shared
worktree, with another session mid-edit. That stashed *their* in-flight `recall.py` work
along with mine. The `pop` restored it and I verified their diff was intact, but if they had
written to those files in between, the pop could have conflicted and lost work.

`git stash` is a whole-tree operation, and the whole tree is not mine. The equivalent
question — "were these lint errors already here?" — is answerable with `git stash` in a
private checkout and needs `git show HEAD:path | ruff check -` in a shared one. Same class of
mistake as the file overwrite: an operation whose blast radius is wider than the thing I was
reasoning about.

## 74. Every recorded defect is fixed — what changed, and what that does to the verdict

All thirteen are closed and the suite carries **zero `xfail` markers**, down from 31. Split
seven / six between the two sessions. §49b's ordering held: `EXC-19c` first, then `SCH-14`
(whose migration the other session applied to the real profile databases after backing them
up and confirming no duplicates existed), then `RTV-12`.

**The verdict's three blockers are gone, so the answer to "is it ready" is now unqualified
for the canonical layer.** What has *not* changed is the scope: the ~56,000 lines outside
the memory layer still have no tests, live model quality is still unverified by design, and
the older workspace subsystems still had only a working-order pass — which found two of the
thirteen defects in the first hour, so the ratio there remains a reason to look further.

**Two fixes were decisions rather than repairs**, and both are worth reading as such:

`PRE-01b` asked for "call me X" *and* the bare copula ("I'm Soham") to be deterministic. I
implemented the first and deliberately refused the second. A capitalisation heuristic cannot
tell "I am Soham" from "I am British", "I am Muslim" or "I am Deaf" — I checked, and it
matches all four identically. Two of those are sensitive categories, and the deterministic
path is precisely the one that does not consult the model's judgement about what a statement
means. Falling through is the safer outcome, so the test now asserts it as intended
behaviour rather than recording it as a gap. Fixing the half that was unambiguous and
declining the half that was not is the whole of the engineering judgement here.

`RCL-31b` was a contradiction, not a bug: `USAGE_AFFECTS_RANKING = False` was declared and
never read, while the scorer gave usage a 0.03 weight. The other session resolved it toward
the declared policy and folded the freed weight into the lexical term, so the maximum stays
1.0 and `recall_min_score` keeps meaning what it meant. That second part is what makes it
safe — a naive removal would have silently lowered every score and changed which memories
clear the threshold.

## 75. The stemmer fix moved a boundary, which is a bigger change than it looks

`RCL-21d` needed `-es` plurals and undoubled consonants. Both are local rules. The part that
was not local: the length guard sat at five characters, so "runs" was returned unstemmed
while "running" folded to "run" — the pair could never agree no matter what else changed.
Moving the guard to four is a behaviour change for every four-letter token in the corpus.

It is safe for a reason worth stating: stemming is symmetric. It runs over the query and the
stored text alike, so a word it folds imperfectly still matches itself. The risk is not
mangling, it is *collision* — two unrelated words landing on one stem — and four-letter
words are short enough that the folds are shallow. `RCL-21b` pinned the old boundary and now
states why it moved, with the four-letter plurals asserted separately so the change reads as
deliberate.

The over-stemming guards mattered more than the new rules: without excluding `s`, `l`, `f`
and `z` from undoubling, "passing" would fold to "pas" and "falling" to "fal". Those are in
the tests because I checked them, not because a plan item asked.

## 76. A count I nearly reported because it was satisfying

Updating the plan header after the last fix, I wrote **880 of 880** — every item covered,
the round number the whole exercise had been heading toward. The checkboxes said 875 done
and 5 partial.

The five were partial because each described behaviour the code did not have, recorded as an
`xfail`. Four of them are genuinely covered now: `INF-06` (the frozen clock finally has
consumers), `EXC-17` (PRV-02 covers the at-rest half), and `EXC-19` and `OBX-15`, which were
the defects themselves. The fifth, `EXC-14`, is not — the similarity-threshold boundary is
covered for the duplicate finder (`RUN-19`) but not through the coordinator, which is what
the item actually asks for.

So the honest number is **879 covered, 1 partial, 0 open**, and the header says that.

This is the same failure this file documents in four other places — a self-consistent number
with no second source — and it is the one I came closest to shipping, because it was the
number I wanted. The check that caught it was the one that has caught every other instance:
recount from the checkboxes and compare against the sentence, rather than trusting the
sentence.
