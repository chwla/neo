# Neo readiness review — findings and decisions

Ongoing review of the codebase **excluding the memory layer** (`app/services/memory/`,
`app/repositories/memory.py`, `app/api/routes/memory*.py`, `app/db/memory_migrations.py`),
which is deliberately left untouched.

Working method: reproduce first, fix second, re-verify third. Every fix below has a
reproduction that failed before the change and passes after it, plus a happy-path check
proving the fix did not break the working case.

- **Started:** 2026-08-14
- **Branch:** `memory-layer-tests`
- **Last updated:** iteration 25 (search orchestration; sweep complete)

> **Testing standard.** A feature is not "tested" because a sweep did not return 500.
> Broad sweeps only find crashes. Each feature additionally needs meaningful state
> transitions, isolation, security boundaries, error recovery, persistence and realistic
> end-to-end behaviour. Every defect fix carries a permanent regression test in `tests/`,
> and each security test is mutation-checked: the fix is removed and the test must fail.

---

## 1. Summary

Twenty-five review iterations over the non-memory codebase. **13 defects found, fixed and
covered by regression tests**; three decisions remain open for the maintainer (section 4).

Defect yield fell to zero after iteration 14 and stayed there for eleven iterations, by which
point coverage was complete. The later iterations added regression protection rather than
finding bugs — worth having, but the sweep itself is finished.

| # | Defect | Severity |
|---|---|---|
| D1 | Evaluation tables were never created for a profile (500s) | high |
| D2 | Unknown workspace ID returned 500 instead of 404 | medium |
| D3 | Unknown continuity bundle ID returned 500 instead of 404 | medium |
| D4 | GET requests wrote orphan rows for a nonexistent workspace | medium |
| D5 | SSRF guards allowed RFC 6598 shared address space (100.64.0.0/10) | low–medium |
| D6 | DNS rebinding bypassed both SSRF guards | medium |
| D7 | `read_only` sandbox commands could execute anything and delete files | high |
| D8 | one approval could authorise many executions (TOCTOU) | medium |
| D9 | one connector approval could fire many external writes (TOCTOU) | medium-high |
| D10 | one coding-agent approval could apply the same patch repeatedly (TOCTOU) | high |
| D11 | workspace writes reported success for a workspace that did not exist | low |
| D12 | no repository under a user's home could be registered on macOS | high |
| D13 | deleting a chat cost ~30 seconds because it probed the model first | medium |

The three highest-risk chains (coding, connectors, chat) and the production Docker image
are verified working end to end. `pytest` runs 402 tests; two more are opt-in because they
need a live model or live search.

---

## 2. Baseline

| Measure | Value |
|---|---|
| Python files in `app/` | 393 (~76.5k LOC) |
| API paths (OpenAPI) | 320 across ~40 feature areas |
| Import + `create_app()` | ~1.55 s |
| Warm request (`GET /api/notes`, median of 20) | 3.4 ms |
| `ruff check app/` | 21 pre-existing errors (16 in memory-layer files) |
| Stack | FastAPI 0.141.1, Starlette 1.6.0, SQLAlchemy 2.0.51, Pydantic 2.13.4, Python 3.14.6 |

Testing runs against a throwaway `NEO_DATA_DIR`, so real profiles and databases are never
touched.

---

## 3. Defects found and fixed

### D1 — Evaluation tables were never created for a profile (500s)

**Severity:** high — two endpoints returned HTTP 500 on any fresh profile.

`ensure_profile_storage()` in [profile_accounts.py](app/services/profile_accounts.py)
initialises the tables for 26 features but omitted evaluation entirely. Only
`store.suites()` and `store.create_suite()` call `initialize_evaluation_tables()` lazily,
so `runs()` and `baselines()` hit missing tables.

The failure was **order-dependent**, which is why it is easy to miss: opening the evaluation
UI at `/suites` first created the tables as a side effect and hid the bug. Loading
runs or baselines first — what a dashboard panel actually does — crashed.

```
GET /api/evals/runs       -> OperationalError: no such table: workspace_eval_runs
GET /api/evals/baselines  -> OperationalError: no such table: workspace_eval_baselines
```

**Fix:** added `initialize_evaluation_tables` to the `ensure_profile_storage` initialiser
list, matching the 26 features already there.

**Why there and not in the store:** `ensure_profile_storage` runs on *every* unlock, not just
profile creation, and every initialiser is `CREATE TABLE IF NOT EXISTS`. So this also repairs
already-existing profiles on their next login — no migration needed. Adding lazy
`initialize_*()` calls to each of the five read functions instead would have been more code,
repeated work on every call, and inconsistent with all other features.

---

### D2 — Unknown workspace ID returned 500 instead of 404

**Severity:** medium.

`WorkspaceService.get()` returns `None` for an unknown ID; `generate_plan()` and `report()`
dereferenced it immediately (`workspace["goal"]`), raising
`TypeError: 'NoneType' object is not subscriptable`.

Affected: `POST /api/workspaces/{wid}/plan`, `GET /api/workspaces/{wid}/report`.

**Fix:** route-level 404 guards, matching the convention already used by `GET /{wid}` in the
same file. Sibling service methods `readiness()` and the summary at
`service.py:416` were already written defensively — these two were the outliers.

---

### D3 — Unknown continuity bundle ID returned 500 instead of 404

**Severity:** medium.

Four endpoints crashed on an unknown bundle: `manifest` dereferenced `None`, while
`references`, `validation` and `report` let the service's own
`LookupError("Continuity bundle not found.")` escape untranslated.

**Fix:** a `None` guard on `manifest` and `LookupError -> HTTPException(404)` on the other
three, following the existing `ValueError -> 400` translation already in that router.

**Rejected alternative:** a global exception handler for `LookupError`. `LookupError` is the
base class of `KeyError` and `IndexError`, so it would silently convert genuine bugs
anywhere in the app into 404s.

---

### D4 — GET requests wrote orphan rows for a nonexistent workspace

**Severity:** medium — data integrity.

`GET /api/workspaces/{wid}/readiness` and `/health` recompute readiness checks and **insert
them**, with no check that the workspace exists. A GET on a ghost ID left 9 orphan rows in
`workspace_orchestration_readiness_checks` — verified by row count before and after:

```
before: 0 rows -> 4 GETs on a nonexistent workspace -> after: 9 rows
```

Beyond violating GET semantics, this lets any crawler or scan accumulate junk rows.

**Fix:** 404 guards on `/readiness` and `/health`, and on `POST /{wid}/readiness/recompute`,
which creates the same orphan rows for a missing workspace.

---

### D5 — SSRF guards allowed RFC 6598 shared address space (100.64.0.0/10)

**Severity:** low–medium — security hardening.

Neo has two independent SSRF guards: `validate_connector_url` for user-configured
connectors, and `is_public_http_url` for web-search page fetching. Both block private,
loopback, link-local, reserved, multicast and unspecified addresses — but **both allowed
carrier-grade-NAT space**, because Python's `is_private` returns `False` for 100.64.0.0/10
and neither guard's explicit network list covered it.

Measured across special ranges — everything else was correctly blocked:

```
100.64.0.1       is_private=False   connector=ALLOWED  web-fetch=ALLOWED   <-- gap
198.18.0.1       is_private=True    connector=blocked  web-fetch=blocked
192.0.2.1        is_private=True    connector=blocked  web-fetch=blocked
240.0.0.1        is_private=True    connector=blocked  web-fetch=blocked
169.254.169.254  is_private=True    connector=blocked  web-fetch=blocked
```

100.64.0.0/10 is not routable on the public internet and is used for ISP carrier-grade NAT
and by some cloud/container networks for internal addressing, so it is a legitimate
pivot target.

**Fix:** added the range to `PRIVATE_NETWORKS` in
[search/security.py](app/services/search/security.py) and as `SHARED_ADDRESS_SPACE` in
[tools/security.py](app/services/tools/security.py). Boundary addresses either side of the
range (100.63.255.255 and 100.128.0.1) were verified to remain allowed, so the change does
not over-block.

---

### D6 — DNS rebinding bypassed both SSRF guards (was R6)

**Severity:** medium. **Fixed; this was a launch blocker.**

Both guards resolved a hostname to validate it, then handed the *hostname* to `requests`,
which resolved it again when connecting. Nothing pinned the approved address, so an
attacker-controlled record with a short TTL could answer "public" during validation and
"private" at connect time. Proven end to end against a live listener before the fix:

```
resolutions handed out : ['93.184.216.34', '93.184.216.34', '127.0.0.1']
[listener] ACCEPTED a connection from ('127.0.0.1', 64701)
```

**Fix:** the addresses approved by validation are pinned for the calling thread, and the
resolver returns only those while the request is in flight
([tools/security.py](app/services/tools/security.py)). Pinning *by address* rather than
rewriting the URL to an IP is the key decision: the hostname stays in the URL, so TLS SNI,
certificate verification and the `Host` header all keep working untouched. The same pin is
applied to the web-fetch path ([search/content.py](app/services/search/content.py)), which
shares the root cause and is arguably more exposed, since search results are
attacker-influenced while connectors are at least user-added.

Verified on both sides of the boundary, as required:

- **Attack blocked** — with DNS rebinding to loopback, the private listener is never
  reached, on both the connector and web-fetch paths.
- **Normal HTTPS still works** — a live `https://example.com` request returns 200 with the
  expected body, proving TLS/SNI/cert verification survived. Plain HTTP to trusted
  localhost is covered offline.
- **Pin is thread-local** — a pin in one thread does not affect another's resolution.
- **Mutation-checked** — with pinning neutralised, both rebinding tests fail (the listener
  is reached). An earlier version of the web-fetch test passed even with the fix removed;
  it counted DNS resolutions, and the count never reached the rebind threshold. It now keys
  off `port is None` (validation) versus a real port (connect), which does not depend on how
  many times the internal validation paths resolve.

**Files changed (14):**
[profile_accounts.py](app/services/profile_accounts.py),
[workspaces.py](app/api/routes/workspaces.py),
[continuity.py](app/api/routes/continuity.py),
[tools/security.py](app/services/tools/security.py),
[search/security.py](app/services/search/security.py),
[search/content.py](app/services/search/content.py),
[command_sandbox/policy.py](app/services/command_sandbox/policy.py),
[command_sandbox/store.py](app/services/command_sandbox/store.py),
[command_sandbox/service.py](app/services/command_sandbox/service.py),
[tools/store.py](app/services/tools/store.py),
[tools/executor.py](app/services/tools/executor.py),
[coding_agent/store.py](app/services/coding_agent/store.py),
[coding_agent/orchestrator.py](app/services/coding_agent/orchestrator.py),
[repos/safety.py](app/services/repos/safety.py).

---

### D7 — `read_only` sandbox commands could execute anything and delete files

**Severity:** high — the most serious defect found so far. Confirmed by execution, not
just by reading policy.

The command sandbox is an allowlist over `subprocess` with `shell=False`, which correctly
blocks shell injection. But the `FORBIDDEN` executable set and the shell-metacharacter
check only inspect **`command[0]`** and argument *syntax*. Several allowlisted tools spawn
processes or mutate the filesystem through their own flags, needing no shell at all — so
they slipped past every check in the category named `read_only`:

```
find . -name victim.txt -delete           -> allowed; file really was deleted (exit 0)
find . -type f -exec touch PWNED.txt {} + -> allowed; arbitrary command really ran
rg --pre sh pattern                       -> allowed; runs a preprocessor command
```

Both destructive cases were executed against a temporary workspace to confirm they were
real: `victim.txt` was gone, and `PWNED.txt` was created by a command the allowlist never
authorised. `-exec` also defeats the `FORBIDDEN` list entirely — `rm`, `curl` and `sh` are
all reachable as `find` arguments.

**Fix:** a per-executable flag denylist (`DANGEROUS_FLAGS`) in
[policy.py](app/services/command_sandbox/policy.py), covering `find`'s process-spawning and
file-writing primaries (`-exec`, `-execdir`, `-ok`, `-okdir`, `-delete`, `-fprintf`,
`-fprint`, `-fprint0`, `-fls`) and `rg`'s command-running flags (`--pre`,
`--hostname-bin`). Both `--flag value` and `--flag=value` spellings are matched.

**Why a denylist here, when allowlists are otherwise preferred:** the allowlist already
exists and operates on the executable. This closes the gap between "which program may
run" and "what that program may be told to do". Enumerating the safe flags of `find`
instead would be a far larger change and would break ordinary searches.

Verified on both sides: all 13 escape variants are rejected, and 13 ordinary read-only
commands (`ls -la`, `find . -name '*.py'`, `grep -r`, `rg --glob`, `cat`, `head`, `wc`,
`tree`) still pass, as do the `test`-category commands. Confirmed through the real API,
not only the policy function. 47 tests in
[test_command_sandbox.py](tests/app/test_command_sandbox.py); the 14 escape tests fail
against the pre-fix policy.

**Note on residual risk:** the `test` and `build` categories run `pytest`, `npm run build`
and similar, which execute project code by design. That is inherent to the feature, not a
policy hole — but it does mean sandbox approval should be treated as "may run this
repository's code", and the approval step (which remains mandatory for every command) is
what carries that weight.

---

### D8 — one approval could authorise many executions (TOCTOU)

**Severity:** medium — breaks the sandbox's central guarantee.

`CommandSandboxService.execute` checked `status == "approved"` and then updated the row to
`running` in a **separate** statement. Concurrent callers all passed the check before any
of them wrote, so a single approval executed once per caller. Measured with six concurrent
calls on one approved run:

```
results: ['completed'] x6      actual executions of the command: 6
```

Every other approval gate held under test — executing without approval, approving without
confirmation, and approving a policy-blocked command are all correctly refused. The hole
was only in the final check-then-act step.

**Fix:** an atomic `claim_for_execution` in
[store.py](app/services/command_sandbox/store.py) that selects and transitions the run
inside `BEGIN IMMEDIATE`, returning `None` to every loser. This is the same pattern the
codebase already uses for `consume_oauth_state`, which was verified in iteration 3 to admit
exactly one winner from sixteen racing threads — so the fix follows an in-repo precedent
rather than inventing one.

The workspace and `cwd` are still resolved *before* the claim, so an invalid `cwd` fails
without leaving a run stranded in `running`.

Verified with the window deliberately widened to 300 ms and eight concurrent callers over
five trials: exactly one execution and exactly one winner every time. The single-caller
path still works and a second execute is correctly refused.

---

### D9 — one connector approval could fire many external writes (TOCTOU)

**Severity:** medium-high — same class as D8, but the side effects leave the machine.

Found by deliberately looking for D8's shape elsewhere. `ToolsService.approve_call` read
the call, checked `approval_status != "pending"`, then updated it in a **separate**
statement before executing. Concurrent approvers all passed the check and each ran the
tool. Connector tools perform external writes — REST `POST`, destructive MCP operations —
so a duplicate execution is a duplicate real-world action (a second payment, a second
ticket, a second webhook).

Reproduced against a live server with eight concurrent approvals of one pending call:

```
uninstrumented   : 4 of 8 approvals succeeded   (intermittent, timing-dependent)
widened window   : 8 of 8 approvals succeeded, tool executed 8x   (3/3 trials)
```

**Fix:** `claim_call_for_approval` in [tools/store.py](app/services/tools/store.py),
selecting and transitioning the call inside `BEGIN IMMEDIATE`, with `approve_call`
refusing when the claim returns `None`. Same remedy as D8 and the same in-repo precedent
(`consume_oauth_state`).

**The rest of the approval model held.** Verified by test: a write tool never executes
before approval; rejection is terminal and a rejected call cannot later be approved; a
second approval of a completed call is refused; and credentials in tool arguments are
refused — recorded as a `blocked` call with an audit trail rather than silently dropped.

**Note on the builtin `create_note` tool:** it is a stub that records
`"Approved workspace write recorded; no automatic write performed."` and writes nothing.
That is why the race showed no duplicate notes and had to be measured at the executor
boundary instead. Worth knowing before trusting builtin write tools in a demo.

---

### D10 — one coding-agent approval could apply the same patch repeatedly (TOCTOU)

**Severity:** high — the most destructive instance of this pattern.

`CodingAgentOrchestrator.approve` read the action, called `require_pending`, then updated
and executed it in separate steps. Action types include `apply_patch`, so concurrent
approvers each applied the same patch to the repository. Measured with six concurrent
approvals of one pending action:

```
apply_patch executions: 6   (from a single approval)
```

**Fix:** `claim_action_for_execution` in
[coding_agent/store.py](app/services/coding_agent/store.py) using `BEGIN IMMEDIATE`, with
`approve` refusing when the claim returns `None`. Mutation-checked: reverting the fix makes
the test fail with 6 executions.

---

### D11 — workspace writes reported success for a workspace that did not exist

**Severity:** low — correctness, not a data leak.

`PATCH /api/workspaces/{wid}` returned **`200 null`** and `DELETE` returned **`204`** for an
unknown identifier, including one belonging to another profile. Isolation itself was intact
(per-profile databases mean the `UPDATE` matched zero rows and nothing of the other
profile's changed — verified), but the API told the caller the write had succeeded. A
frontend would show an edit as saved when nothing happened.

Same family as D2, and inconsistent with `GET /{wid}` in the same file, which already 404s.
Fixed with the same route-level guard.

---

### D12 — no repository under a user's home could be registered on macOS

**Severity:** high — a launch blocker on the platform this is being developed on.

`SYSTEM_ROOTS` in [repos/safety.py](app/services/repos/safety.py) is matched against a
candidate **and all of its parents**, and it contained `/Users`. On macOS every project
lives under `/Users/<name>/...`, so every real repository was refused:

```
/Users/chwla/Desktop/neo  ->  "System directories cannot be registered as repositories."
```

That is this checkout. With no repository registerable, everything built on one —
repository browsing, code index, symbols, the test runner, Git checkpoints and the whole
coding agent — was unusable on macOS. The API sweeps never caught it because registration
returns a clean 400: it looks like correct validation until you notice *what* is being
refused.

**Fix:** `/Users` (and `/home`) moved to a separate `ACCOUNT_CONTAINERS` set matched
**exactly**, never against parents. Registering the container itself is still refused, with
a clearer message. The genuine system trees keep subtree matching.

Verified end to end: registering this repository through `POST /api/repos/register` now
returns 201, while `/Users`, `/etc`, `/usr/share`, `$HOME` and the filesystem root are all
still refused, as are non-existent paths, files, and symlinked roots.

---

### D13 — deleting a chat cost ~30 seconds because it probed the model first

**Severity:** medium — user-facing latency on a routine action.

`delete_chat` built the memory runtime and *then* looped over the chat's messages looking
for user messages whose sources need detaching. Building that runtime probes the configured
model (the R1 cold-start path), so deleting a chat with **no** user messages paid the full
probe for a loop body that never ran:

```
DELETE an empty chat (cold process) -> 204 in 30.6 s
DELETE a second empty chat          -> 204 in 0.01 s   (probe result cached)
```

**Fix:** collect the user messages first and build the runtime only when there is at least
one to detach ([chat.py](app/api/routes/chat.py)). Behaviour is unchanged whenever there is
real work to do -- the same runtime is built and the same detach runs.

This is a chat-route change, not a memory-layer change: it alters *when* chat calls into
memory, never what memory does.

Verified both directions: an empty chat no longer builds the runtime at all, and a chat
with a user message still builds it and still detaches that message's sources. The chat
test file dropped from 29 s to 3 s as a side effect.

---

### Audit: the check-then-act pattern, systematically

After D8 and D9 turned up the same shape twice in two attempts, guessing site by site was
clearly the wrong method, so every one-shot side-effect gate outside the memory layer was
reviewed deliberately. The result:

| Site | Gate style | Verdict |
|---|---|---|
| `tools.approve_call` | stored pending -> approved | **D9 — raced, fixed** |
| `command_sandbox.execute` | stored approved -> running | **D8 — raced, fixed** |
| `coding_agent.approve` | stored pending -> approved | **D10 — raced, fixed** |
| `oauth.consume_oauth_state` | stored, `BEGIN IMMEDIATE` | already correct (verified iteration 3) |
| `test_runner.run_command` | per-invocation `confirm` | not susceptible — inserts a new run per call |
| `git.restore` | per-invocation `confirm` | not susceptible — no stored pending state, and restoring twice is idempotent |

The distinction that matters: gates carrying a **stored** pending state need an atomic
claim, while gates taking confirmation **per invocation** create a fresh record each time
and cannot be double-spent. All three defective sites now use the same `BEGIN IMMEDIATE`
claim the OAuth consumer already used, so the codebase has one pattern rather than two
correct implementations and three broken ones.

---

## 4. Open decisions — these need you

### R2 — `/api/health/ready` always reports 503 on a default install

`web_search_provider` defaults to `"disabled"`, and the readiness check treats "disabled" as
a failure ([health.py:118](app/api/routes/health.py#L118)), so a default local install is
permanently "not_ready".

**Impact is limited:** Docker's `HEALTHCHECK` uses `/api/health/live`, not `/ready`, and the
Dockerfile sets `NEO_SEARCH_PROVIDER=duckduckgo`. Nothing in the repo currently gates on it.

Two defensible readings, and they lead to different code, so this is **your call**:
1. Search is core; 503 is correct and the default should ship configured.
2. Search is optional (it is off by default); readiness should only reflect what is needed to
   serve traffic, and search belongs in the payload as informational.

Separately, and regardless of which you pick: the readiness check runs a **live search query**
(`"Neo assistant readiness check"`) on every call. A probe on a 30 s interval would issue a
real external search every 30 s, burning quota and rate limits.

---

### R3 — `tests/` and `docs/` are deleted in the working tree

`git status` shows 52 deleted files — the entire `tests/memory/` suite (~22k lines),
all of `docs/`, and `decisions.md` — deleted on disk but still in git.

I have not restored them: they are memory-layer tests and docs, which you told me to leave
alone. Note this means **the repo currently has no runnable test suite**, and `pyproject.toml`
still points `testpaths` at `tests`. Recover with `git restore tests docs decisions.md`
whenever you want them back.

---

## 5. Known behaviour, deliberately unchanged

### R8 — common currency phrasings are not routed to the live rate provider

**Not changed — this is a precision/recall decision for you.**

Currency intent fires on only two shapes:

```
convert 100 USD to EUR       -> currency  (amount 100, USD -> EUR)
how much is 100 USD in EUR   -> currency
```

These natural phrasings all fall through to `none`:

```
100 USD to EUR
150 usd in gbp
exchange 100 USD to EUR
100 dollars to euros
what is 100 USD in EUR
```

Falling through means the query is answered by the model, and a model answering an
exchange-rate question answers it from training data — a confidently stated, certainly
stale rate. That is the one case where conservative routing is arguably worse than a false
positive, because the fallback is not "no answer" but "wrong answer".

Against that: a looser pattern like `<number> <CCY> to <CCY>` would fire on ordinary prose
("I paid 100 USD to the vendor"), and widening a classifier trades precision for recall in
a way only you can weigh. The gap is pinned by
[test_live_data_routing.py](tests/app/test_live_data_routing.py), which asserts the current
behaviour and documents it as a gap rather than as correct, so the test will fail loudly if
the patterns are ever widened.

---

### R1 — First memory request blocks ~31 seconds

**This is the single biggest obstacle to real-world use that I found**, but it is in the
memory layer, so it is reported rather than fixed.

On a cold process the first `GET /api/memory` takes **31.2 s**; the second takes **22 ms**.
Stack sampling during the stall shows the cause precisely:

```
memory.py:207 list_memories -> factory.py:202 build_memory_runtime
  -> factory.py:122 _resolve_ollama_request_mode
  -> extraction.py:944/947 probe_ollama_provider   (two probe POSTs)
  -> socket.readinto  <-- blocked here
```

`memory_ollama_request_mode` defaults to `"auto"`, so the runtime probes Ollama twice to
detect the request format. With `qwen3-coder:30b` configured, those probes force a cold load
of a 30B model. The result is cached, so only the first request pays — but that first request
is the one a user makes right after logging in.

*Mitigation available without touching memory code:* set `NEO_MEMORY_OLLAMA_REQUEST_MODE`
explicitly to skip the probe. Say the word and I will confirm the correct value and whether
a smaller warm-up model or a bounded probe timeout is the better fix.

**Measured scope (iteration 14).** This is a one-off cost per process, not a per-request
one. Three sequential chat messages on the configured qwen3-coder:30b:

```
message 1: 32.2 s   (cold model load)
message 2:  2.0 s
message 3:  2.0 s
```

So chat itself is responsive once warm. An earlier single measurement of 501 s for one
reply was an outlier -- a cold 18.6 GB model load competing with other work -- and should
not be read as representative. The real cost is ~30 s once, paid by whichever request
happens to be first. D13 was one such request that did not need to pay it at all.

---

### R7 — Redundant condition in the connector guard

[tools/security.py](app/services/tools/security.py) gates the trusted-localhost escape hatch
with `not (localhost_name or loopback_only) or not loopback_only`. The `localhost_name` term
can never change the result — the expression reduces to `not loopback_only`. Behaviour is
correct and appropriately restrictive; it is just dead logic. Left alone as unrelated to any
defect, per the surgical-changes rule.

---

### R4 — Pre-existing lint, left alone

`ruff check app/` reports 21 errors, 16 of them in memory-layer files. The 2 in
`profile_accounts.py` (a long line at :401, an unsorted import block at :540) predate my
change — both appear in the baseline run before any edit. Left untouched per the
"don't improve adjacent code" rule.

---

### R5 — `@app.on_event("shutdown")` is deprecated

[main.py:189](app/main.py#L189) uses the deprecated `on_event` hook to clean up guest
profiles. Verified it **still fires** on FastAPI 0.141 — so this is a future-upgrade risk,
not a current bug. It will need to become a lifespan handler eventually.

---

### Decisions where I chose *not* to act

**Left SQLite write latency under heavy concurrency alone.** At 24 concurrent writers to one
profile, write latency degrades to p50 169 ms / p95 1079 ms — SQLite serialises writers by
design. No errors, no lost writes. For a local single-user assistant, 24-way concurrent
writing is not a real workload, so tuning for it would be optimising a scenario that does not
occur. Recorded as a known characteristic, not a defect.

**No speculative performance work.** Warm requests are 3.4 ms and startup is 1.55 s. I
profiled rather than assumed, and found no per-request bottleneck, so I changed nothing. The
one real latency problem is R1, and it is in the layer I was told to leave alone.

**Left `ensure_profile_storage` re-running on every login.** It costs 78 ms (measured;
the other ~56 ms of a 140 ms unlock is PBKDF2 at 390k iterations, which is intentional and
correct). It is idempotent, per-login rather than per-request, and it is exactly the mechanism
that lets the D1 fix repair existing profiles. Caching it would save 78 ms once per login and
break that property.

**Left 12 endpoints that return `200` with an empty collection for a missing parent ID**
(e.g. `/api/tasks/{task_id}/agent-runs`, `/api/web-search/runs/{run_id}/evidence`). An empty
list for an absent parent is a defensible API contract, and none of them write to the
database. Only the two that *wrote* rows (D4) were treated as defects.
`GET /api/continuity/bundles/{bid}` returning `null` with 200 is the weakest of these — it is
now inconsistent with its own sub-routes, which 404. Worth aligning if you want it.

---

## 6. Verified sound

Attacked or exercised, not merely read. Recorded so these are not re-litigated later.

### Frontend build and serving — previously untested end to end

The React build and the way the API process serves it had never been exercised. Both now
are:

* `npm run build` succeeds — 67 modules, 433 kB JS (116 kB gzipped), ~1 s.
* `npm test` (the existing frontend suite) passes, 12/12.
* The API serves the real `dist/`: index at `/`, hashed assets under `/assets/`, SPA deep
  links falling through to the app shell, `index.html` returned uncached so an upgrade is
  picked up, `/api/*` still 404ing rather than being swallowed by the catch-all, and the
  legacy service worker retiring itself.

Covered by [test_frontend_serving.py](tests/app/test_frontend_serving.py), which skips
cleanly when `frontend/dist` has not been built so a backend-only checkout still runs.

---

### End-to-end coding journey — the whole chain now verified working

D12 changed the method here. Per-endpoint sweeps had run clean for eleven iterations while
the single most important feature chain was completely unreachable on macOS, because the
blocking step returned a well-formed `400`. Status codes cannot answer "can a user actually
do this?" -- only walking the journey can.

The full sequence a user follows is now exercised in order, each step feeding the next:

```
register a repository        -> 201, managed copy created (never the original checkout)
browse repository files      -> calc.py present
build + search code index    -> 200
build symbol index, find def -> 200
detect test commands         -> suggests pytest for pyproject.toml + tests/
create a command, run it     -> status "passed", exit 0, real pytest output
git init -> edit -> checkpoint -> 201, checkpoint listed
```

Nine tests in [test_coding_journey.py](tests/app/test_coding_journey.py), including that
the managed copy is separate from the user's checkout, that running tests requires
confirmation, and that repositories are isolated between profiles.

**Two behaviours confirmed deliberate, not defects:**

* `/test-runner/repos/{id}/detect` returns *suggestions* and persists nothing; creating a
  command is a separate explicit step. Listing commands straight after detect correctly
  returns an empty list.
* Checkpointing a clean tree is refused with "nothing to checkpoint" -- correct, since
  `git init` commits everything.

Both looked like failures on first read of the journey output, and both were checked
against the code before being written off.

---

### Code intelligence and live search — both correct

Index and symbol resolution were checked for *correctness*, not just non-empty responses,
against a repository with known contents:

```
index            -> 2/2 files, status ready
symbols captured -> add, Calculator, multiply, caller, use
definition "add" -> src/calc.py, function, line_start 1  (the actual declaration line)
references "add" -> 3, spanning src/calc.py and src/other.py (import + both call sites)
missing symbol   -> resolves to nothing rather than a wrong guess
```

Live provider search also works: a DuckDuckGo query returned three real results in 1.0 s,
and a full `web-search/run` completed with 2 sources and 2 evidence items in 0.9 s. This is
also the practical answer to R2 -- with a provider configured, search is healthy; the 503 is
purely about the *default* being `disabled`.

Nine offline tests in [test_code_intelligence.py](tests/app/test_code_intelligence.py);
the live-provider check is opt-in via `NEO_TEST_LIVE_SEARCH=1`.

---

### Search provider selection — ordering and fallbacks are correct

Pure configuration logic, verified offline. It matters operationally: a mis-built chain
either skips the provider a user configured, or quietly reaches for one needing an API key
they never supplied.

```
primary + fallbacks      -> primary first, fallbacks in the order given
primary "disabled"       -> [Disabled] only; no fallback used behind the user's back
search disabled globally -> [Disabled] only
primary repeated in list -> de-duplicated
"disabled" as a fallback -> ignored
whitespace / mixed case  -> normalised
unknown provider name    -> Disabled, never an exception mid-search
tavily as a fallback     -> dropped unless it is the chosen primary (it needs a key)
```

Result normalisation is equally careful: duplicate URLs collapse, rows without a URL are
dropped, the result limit is honoured, and an empty result set is reported as
`"Search returned no results."` rather than a successful-looking empty list — so a caller can
tell "nothing found" from "search worked".

**No defects.** This was the last thin surface outside the memory layer.

### Citation discipline — the grounding promise holds in chat, not just in research

The README's promise is specific: "Search-result snippets help discovery only; claims and
citations require successfully fetched page content." Iteration 17 showed *research* honours
that. This checks the **chat** path, which is where a user actually meets it.

The rule lives in `validate_citation_markers`, which builds its citable set as
`{c.index for c in citations if c.fetched}` — a snippet-only source is simply not available
to cite. Verified across the whole boundary:

```
citation to a fetched page          -> accepted
citation to a snippet-only source   -> rejected ("Unknown citation indices")
mixed sources                       -> only the fetched subset is citable
index that does not exist           -> rejected
fetched but not evidence-supporting -> rejected via supported_indices
sources available but no marker used-> rejected ("no verified citation markers")
marker floating free of a sentence  -> rejected as orphaned
```

The chat dispatcher is **stricter** than the validator alone: it passes
`supported_indices={chunk.source_index for chunk in evidence_chunks}`, so a citation must
point at a source that actually produced evidence, not merely one that was fetched.

What reaches the transcript is guarded in layers, all confirmed:

* a model-written `Sources:` block is stripped and replaced with the backend's verified list
* URLs the model invented are removed (`_strip_fabricated_urls` against the verified set)
* markers with nothing to attach to are stripped, so a user never sees `[1]` pointing at nothing
* prose that merely contains the word "source" is not truncated — stripping keys on a block
  header, not the bare word

**No defects.** This closes the last substantive gap outside the memory layer.

### Streaming and partial responses — correct, including the failure paths

A user watching a reply appear is reading rows the streaming loop writes, so a dropped,
duplicated or unfenced write corrupts a transcript in front of them. Driven with a scripted
event stream so the whole loop runs offline and deterministically:

```
chunk, chunk, chunk -> accumulated in order, reply matches
replace             -> earlier draft text discarded, not appended
done without reply  -> falls back to the accumulated text (no silent loss)
thinking            -> kept separate; never leaks into the visible answer
status              -> never touches the answer
stream ends w/o done-> marked failed, never left running forever
provider raises     -> marked failed with completed_at set
lease lost mid-stream-> worker stops immediately; later chunks are not written
already completed   -> a rerun does not overwrite the first answer
```

The last three are the ones that matter for a "resumable generation" claim: every event's
write goes through the fenced update and the loop returns the moment it fails, so a
superseded worker cannot keep appending to a reply another worker now owns.

**No defects.**

**Suite-health note.** The first streaming test initially took 32.75 s — the R1 cold model
probe, reached through the memory runtime the worker builds. Since these tests are about
streaming and not memory, the fixture now sets `memory_enabled: false` on the generation,
taking the file from 48 s to 3.1 s. Same reasoning as D13: do not pay for a runtime nothing
in the path needs.

### Generation leases and fenced writes — the README's concurrency claim holds

The README promises "expiring worker leases, fenced writes, and a generation-linked
assistant row prevent duplicate transcript entries across retries, refreshes, and process
restarts". That is a concurrency claim in the same family as D8-D10, so it was attacked
rather than trusted. It holds:

```
8 workers race for one queued generation -> exactly 1 claims it
a running generation with a live lease   -> second worker refused
a lease aged past the cutoff             -> takeover allowed (a crashed worker cannot strand work)
worker one writes after losing its lease -> refused; its text never reaches the row
a second assistant row for one generation-> IntegrityError from the unique index
```

**Mutation-checked**, because a concurrency test that cannot fail proves nothing: replacing
`_claim_generation`'s atomic UPDATE with the read-check-write shape lets **8 of 8** workers
claim the same generation. The real implementation admits exactly one.

**The contrast with D8-D10 is the interesting part.** The chat worker uses a conditional
`UPDATE ... WHERE status = 'queued' OR (lease expired)` and treats `rowcount != 1` as "I lost
the race" — the correct compare-and-set. The three approval gates in the sandbox, connector
executor and coding agent used read-then-write instead. The right pattern was already in the
codebase twice (here and in `consume_oauth_state`); the defects were where it was not
followed, which is why the iteration-8 audit was framed around finding the shape rather than
the symptom.

### Weather assembly and the chat routing glue

Weather is built from two calls (geocode, then forecast) and was verified offline with
injected responses:

```
Paris -> location/country/timezone, 12.5°C, feels 10.0°C, wind 18.2 km/h
weather codes 0/3/61/95 -> clear sky / overcast / slight rain / thunderstorm
unknown location  -> "I could not find a location matching Nowhereville."
malformed forecast-> LiveDataError, never a partially-built report
empty / 101-char location -> rejected before any request
```

The glue in `chat.py` that turns a resolved intent into a reply behaves well on all three
paths:

* **Date/time** is answered from the local clock with `used_web: False` — no network at all.
* **Currency** replies carry provenance, not just a number: the converted amount, the rate,
  the reference date and a `Source:` URL. An unattributable figure would be much harder for
  a user to sanity-check.
* **Provider failure** returns the error text as the reply with
  `finish_reason: "provider_error"` — it does not fall through to the model and does not
  invent a rate. Verified explicitly: the failed reply contains no fabricated number.
* An intent that is not live-data returns `None` and falls through to the model.

**No defects.** Note on method: the failure case initially *looked* wrong because the
response kind stays `structured_currency`; reading further showed the reply text is the
error and `finish_reason` marks it. Reporting that on first glance would have been a false
positive.

### Live-data answers are exact, and fail rather than guess

The weather/currency/date-time paths answer the user directly instead of asking the model,
so correctness matters more than anywhere else. Verified offline with injected dependencies
(`http_get`, `now`), so the results are deterministic:

```
100 USD -> EUR at 0.9        -> 90.0, reference date preserved
same-currency conversion     -> rate 1, provider never called
missing / zero / negative / non-numeric rate -> LiveDataError, never a number
invalid currency codes       -> rejected before any request
provider connection failure  -> "Currency rates are temporarily unavailable."
15:09 UTC as Asia/Kolkata    -> "8:39 PM"  (+5:30, correct)
a date question              -> date only, no clock time
invalid timezone             -> falls back profile -> UTC
```

The refusal cases matter most: a wrong exchange rate presented confidently is worse than an
error, and the client raises rather than returning a number it cannot trust.

Intent routing is correspondingly conservative — `hello`, `write me a python function`,
`who is the president of France` and `explain recursion` all resolve to `none` and go to the
model rather than triggering a live-data call or a web search.

### Research does not invent answers when it has no evidence

The strongest correctness property found in the whole review, and the one most worth
keeping. With the default `disabled` search provider there is nothing to ground a claim on.
Asked *"What is the capital of France?"* -- a fact the configured model certainly knows --
the run completed and reported nothing:

```
status              : completed
overall confidence  : 0.0
claims / evidence   : 0 / 0
report mentions Paris: False
```

So the system refuses to answer from model knowledge when it cannot cite evidence, exactly
as the README promises ("claims and citations require successfully fetched page content").
An assistant that quietly fell back to its own recall here would be far more dangerous than
one that returns nothing, so this is now asserted against a fact the model knows, which is
what makes the test meaningful.

Research *planning* works offline and independently, so a user with no search provider still
gets a structured plan rather than an error.

---

### Agents, agentic runs, recovery

Nine builtin agents (`general`, `planner`, `coder`, `reviewer`, `tester`, `researcher`,
`refactor`, `explorer`, `summarizer`) are seeded per profile with system prompts and types.
Custom definitions round-trip and are isolated between profiles. Agentic runs start, expose
steps, and stop. Recovery surfaces are empty and healthy on a new profile, and recovery
actions against an unknown run are refused. 16 tests in
[test_agents_research.py](tests/app/test_agents_research.py).

---

### Rule profiles cannot weaken the safety gates — verified

Rule profiles configure agent behaviour, which makes them a natural way to try to route
around the approval gates D8-D10 exist to protect. They cannot. `_enforce_safety` in
[rules/resolver.py](app/services/rules/resolver.py) applies a floor after merging:

```
profile sets require_patch_approval=false  -> resolved: true
profile sets require_test_approval=false   -> resolved: true
profile sets patch max_files=500           -> resolved: 8
profile adds its own forbidden path        -> kept, and .env/.git/node_modules/dist/secrets re-added
```

Crucially the overrides are **reported, not silently dropped**:

```
Safety override ignored: require_patch_approval cannot be disabled.
Safety override ignored: require_test_approval cannot be disabled.
Safety override ignored: patch max_files cannot exceed 8.
```

Silent clamping would leave someone believing their configuration had taken effect. This
is good, deliberate design; 11 tests in
[test_rules_resolution.py](tests/app/test_rules_resolution.py) now hold it in place,
including that rule profiles are isolated between account profiles.

Merging itself is additive across scopes (a global and a project profile both contribute)
and resolution records which profiles applied plus a resolution log entry.

---

### Regression re-sweep after fifteen iterations of changes

The original iteration-1 sweeps were re-run against the current tree to catch anything the
permanent suite does not cover:

```
unknown-id sweep     -> 0 defects   (404: 93, 422: 9, 200: 12, 204: 2, 400: 1)
mutating sweep       -> 0 server errors across 164 operations
```

The distribution shifted slightly from iteration 1 (one more 404, one fewer 204), which is
exactly the D11 fix landing.

---

### Production Docker image — built, run and exercised

The Dockerfile is the production deployment path and had never been executed. It now has
been, end to end:

```
docker build              -> succeeded
container start           -> /api/health/live healthy after 4 s
docker HEALTHCHECK        -> reports "healthy"
GET /                     -> 200 text/html (the bundled React build is served)
create profile in image   -> 201
create + list a note      -> persisted and returned
```

So the shipped artefact genuinely runs and serves both the API and the frontend from one
origin, on the image's own Python 3.12 base rather than the 3.14 used for development.
Container and image were removed afterwards.

---

### `build_memory_runtime` audit — D13 was the only speculative call site

After D13, all four non-memory call sites were reviewed rather than assumed:

| Call site | Verdict |
|---|---|
| `delete_chat` | **D13 — built before knowing it was needed, fixed** |
| chat send (`chat.py:415`) | needed: the runtime drives recall/extraction for the reply |
| edit message (`chat.py:1025`) | needed: detaches that message's sources immediately |
| rerun message (`chat.py:1090`) | needed: always detaches the replaced message |

The three remaining sites use the runtime unconditionally, so building it eagerly is
correct there. Recorded so nobody "optimises" them later.

---

### Export/import bundles — archival, not restore

Walked the round trip: export a project -> download a real zip -> validate and import it as
a **different** profile.

Export works and is isolated per profile. Import is deliberately **archive-only**: the
response carries the notice *"Archive-only import: records remain inert and are never
executed"*, and the importing profile's workspace gains nothing. Verified by counting the
target profile's projects before and after.

**Worth knowing before release:** anyone reading "export/import" as backup-and-restore will
be surprised, and the response field `imported_entity_ids` is populated even though nothing
enters the workspace -- it lists what the archive recorded. The behaviour is safe and
deliberate; only the naming invites the wrong expectation. Reported, not changed.

---

### Connector setup journey — verified working, vault confirmed under real use

The second full journey, walked in order: import an OpenAPI document -> tool definitions
appear -> store a credential -> invoke a write tool.

```
import OpenAPI document      -> 201, server + tool definitions created
risk categories from method  -> GET = external_read, POST = external_write_approval_required
write tool invoked           -> pending_approval (never executed)
unexpected input field       -> blocked, "Unexpected input fields"
store api_key_header cred    -> 200, configured: true
secret through the API       -> never echoed (credentials, servers, definitions)
secret in the profile DB     -> not present in plaintext
```

The last two lines are the ones worth having: iteration 2 established by reading that the
vault seals credentials with AES-GCM, but this confirms it against a real stored secret --
the value never appears in any API surface and never lands unencrypted on disk.

Also confirmed: rejection is terminal (a rejected call cannot then be approved), an
incomplete credential is refused rather than half-stored, deletion clears `configured`, and
connectors are isolated between profiles.

**No defects.** Every apparent failure on the first walk was a wrong request shape on my
side -- `POST` instead of `PUT` for credentials, `secret` as an object instead of a string,
a missing `header_name`, and an input field the imported operation never declared. Each was
checked against the schema before being dismissed; the strict-schema rejection in
particular is a security property, not a bug.

---

### File-upload path boundary — attacked, held

Twelve traversal payloads (`../../../../etc/pwned.txt`, backslash and double-encoded
variants, absolute paths, `~/`, NUL bytes, a 300-character name) were uploaded through the
real endpoint. Every one landed inside the profile's `workspace_files` directory with the
path component stripped; nothing was written outside the data directory. `sanitize_filename`
plus `safe_storage_path`'s resolved-parent check are doing their job.

Uploads are content-addressed: identical bytes return the existing record and keep the
first filename. Deliberate and non-destructive, but worth knowing — uploading `gamma.txt`
with the same content as `alpha.txt` hands back a record named `alpha.txt`.

---

### CLI — a shipped entry point that had never been run under test

The ``neo`` console script (~1,360 lines across the CLI and TUI) had no coverage at all.
``main()`` accepts an injected client, so it is testable without a live server, and now is:

* All 26 subcommands parse and their ``--help`` exits 0 with output -- a broken subparser
  now fails here rather than in a user's terminal.
* **Running with no server behaves well**, which is the most common real-world failure: the
  CLI prints ``Neo API unavailable: ...`` and exits 2, with no traceback. HTTP errors print
  status and detail and exit 3. The distinct exit codes matter to anyone scripting it.
* ``--json`` emits valid JSON; the default output is human-readable.
* The commands a user reaches for first (``status``, ``health``, ``rules list``,
  ``tools list``, ``providers status``) each reach the API.

39 tests in [test_cli.py](tests/app/test_cli.py). No defects found -- the CLI's error
handling is genuinely well built.

---

### Test-suite robustness

The live-HTTPS check is the only test that leaves the machine, and one full run hung for
~5 minutes: the request timeout does not cover DNS resolution, so a stalled resolver blocks
indefinitely. It now runs in a worker thread with a hard join deadline and skips instead of
hanging. A suite that can hang is a suite people stop running.

---

### Housekeeping observed, not changed

`frontend/` contains a stray `neo_memory.db` (2.3 MB), its WAL/SHM files and a `profiles/`
directory — the app was started from inside that directory at some point. They are
gitignored and untracked, so nothing will be committed, but they are also copied into no
build output and can simply be deleted. Left alone as unrelated to any defect.

---

## 7. Test suite

The throwaway harnesses have been promoted to a real suite. `pytest` from the repo root
runs it; every test uses a throwaway `NEO_DATA_DIR`, so a run never touches real data.

```
tests/conftest.py                      isolated storage, profile + two-profile fixtures
tests/app/test_connector_ssrf.py       D5, D6 and the static SSRF vectors  (26 tests)
tests/app/test_regressions.py          D1-D4, each failing before its fix  (15 tests)
tests/app/test_notes_tasks_lifecycle.py  behavioural coverage of notes/tasks (26 tests)
tests/app/test_command_sandbox.py      D7 escapes, D8 approval race, boundaries (49 tests)
tests/app/test_tool_approval.py        D9 connector approval race + boundaries (6 tests)
tests/app/test_coding_agent_approval.py D10 patch-approval race + boundaries (4 tests)
tests/app/test_frontend_serving.py     build serving, SPA routing, caching (7 tests)
tests/app/test_workspaces_lifecycle.py workspace behaviour, health, isolation (14 tests)
tests/app/test_projects_lifecycle.py   pin/archive, task links, isolation (14 tests)
tests/app/test_cli.py                  CLI parsing, exit codes, output modes (39 tests)
tests/app/test_repo_path_safety.py     D12 + repo root/traversal boundaries (16 tests)
tests/app/test_coding_journey.py       full register->index->test->commit chain (8 tests)
tests/app/test_connector_journey.py    import->credential->approval chain (11 tests)
tests/app/test_chat_journey.py         chat threads, D13, isolation (13 tests + 1 opt-in)
tests/app/test_bundles_journey.py      export/import archival contract (11 tests)
tests/app/test_rules_resolution.py     rule merging + safety floor (11 tests)
tests/app/test_agents_research.py      agents, agentic, research grounding (16 tests)
tests/app/test_code_intelligence.py    index/symbol correctness (9 + 1 opt-in)
tests/app/test_live_data_routing.py    currency/weather/time, intent + chat glue (51 tests)
tests/app/test_generation_leases.py    lease claim, fencing, duplicate rows (7 tests)
tests/app/test_streaming.py            chunk accumulation, fencing, failures (9 tests)
tests/app/test_citation_discipline.py  fetched-only citations, URL stripping (21 tests)
tests/app/test_search_orchestration.py provider chain, result normalisation (19 tests)
```

**402 passing.** These sit alongside the deleted `tests/memory/` tree (R3) without touching it.

Two lessons worth keeping, both from tests that were wrong before they were right:

* The first web-fetch rebinding test **passed with the fix removed**. It counted DNS
  resolutions and the count never reached the rebind threshold, so it never exercised the
  attack. Mutation-checking caught it; keying off `port is None` fixed it. A security test
  that has not been watched to fail is not evidence.
* Several first-draft assertions encoded my assumptions rather than the API's contract
  (unwrapped `{"note": {...}}` envelopes; treating a body-only note as invalid when the
  product deliberately derives the title from the body). Those were test bugs, not product
  bugs, and were corrected against observed behaviour.

---

## 8. Coverage map

What this review did and did not reach, so the gaps are explicit rather than implied.

**Covered end to end**

| Area | How |
|---|---|
| Accounts, profiles, sessions | isolation, concurrency, persistence |
| Notes, tasks, projects, workspaces | lifecycle, state transitions, isolation, validation |
| Repositories, code index, symbols | correctness of index/definition/references |
| Test runner | detect -> create -> real pytest run |
| Git checkpoints | init, edit, checkpoint, list |
| Command sandbox | escapes, approval race, policy boundaries |
| Connectors | OpenAPI import, credential vault, approval, SSRF guards |
| Coding agent | action approval race, patch-apply gating |
| Chat | threads, generation lifecycle, isolation, live model |
| Rules | merging, safety floor |
| Research / agents / agentic / recovery | grounding, run lifecycle |
| Bundles | export, download, archival import contract |
| CLI | all 26 subcommands, exit codes, output modes |
| Frontend | build, static serving, SPA routing |
| Deployment | Docker build, run, healthcheck, in-container flow |

**Not covered**

* **The memory layer** — excluded by instruction throughout (`app/services/memory/`,
  `app/repositories/memory.py`, `app/api/routes/memory*.py`, `app/db/memory_migrations.py`).
  R1 and the `MemoryBindingError` seen in research-job logs both live here.
* **Chat routing internals** — `app/services/chat.py` is 3023 lines. The chat *journey*, the
  intent classifier, live-data routing, the generation worker's lease/fencing, streaming,
  citation discipline and provider selection are all now covered. What remains is the
  evidence-extraction and ranking heuristics (`ranking.py`, `content.py` extractors), which
  are quality-tuning rather than correctness boundaries.
* **TUI rendering** — `app/cli/tui/` is covered only insofar as `neo tui --help` parses.
* **LSP, GitHub, patches, integration services** — reachable via the API sweeps (no crashes,
  correct 404s) but never given behavioural tests.
* **Evaluation service** — beyond the D1 table fix, suites and scoring are untested.
* **Multi-user load** — SQLite write contention was measured at 24 concurrent writers; no
  sustained or long-running load test was done.

**Two opt-in tests** are excluded from a default run because they leave the machine or need
a warm model: `NEO_TEST_LIVE_MODEL=1` (chat generation) and `NEO_TEST_LIVE_SEARCH=1`
(provider query).
