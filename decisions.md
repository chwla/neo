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
| **`SCH-14`** | **The unique index guaranteeing one active record per exclusive slot does not fire for globally-scoped memories.** The index covers `(owner_id, scope_type, scope_project_id, subject_key, memory_type, domain_key, slot_key)`. Every global record has `scope_project_id IS NULL`, and SQL unique indexes treat NULLs as distinct — so two rows identical in all six other columns are not duplicates as far as the index is concerned. | **Serious.** This is the "one answer per question" guarantee, and it's off for your name, every preference, every current primary goal, current job, and current education. Two contradictory active records can coexist and recall returns whichever ranks higher. Project-scoped records *are* protected, which is what pins the cause. |
| `CON-21b` | An `UpdateMemoryCommand` cannot survive `model_dump(mode="json")` → re-parse. A full dump writes `canonical_value: None`, which `MemoryUpdatePatch` rejects; `exclude_unset` drops the `operation` discriminator instead. Eight of nine commands round-trip; only `update` doesn't. | **Moderate, and reachable.** `mutations.py` stores exactly this dump in `memory_operations.normalized_command_json`, and `execute()` re-parses dicts through the same adapter. So the audit record of an update can't be replayed through the front door that wrote it. |
| `POL-15e` | The sensitive-content address pattern matches a house number followed by `street/road/avenue/lane/drive/boulevard` — but not `Way`, `Court`, `Place`, `Terrace`, `Crescent`, `Close`, `Square`, or `Parkway`. | **Privacy gap.** A home address on a Court or a Way classifies as NORMAL, so it can be stored without the explicit request a home address is meant to require. |
| `NRM-30b` | The negated-fact guard catches `do not want` and `did not want` but not `does not want`. | **Minor.** Display text is normally the user's own first-person words, so this needs a model-written display hint to trigger. |
| `SCH-11b` | The "display text must not be blank" check uses SQLite `trim()`, which strips spaces only. A tab- or newline-only display text passes. | **Cosmetic**, but the constraint doesn't mean what it looks like it means. |

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
