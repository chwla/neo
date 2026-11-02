# Release readiness

Last updated 2026-11-03. Release candidate under test: `a5dcf5735becd866d16c179a13a3591ec2a205f0`
("Profile deletion bug fixed"). Test suite: private repository `chwla/neo-tests` at
`a8a0ee2` (results recorded in `59fb2fa`).

## Verdict: not ready

Every existing automated suite passes on `a5dcf57`, on macOS, Linux and Windows. That is
not release readiness. The release gates require every shipping feature to pass in every
supported platform and browser cell, and most of that coverage has not been built or run
yet (see "Not run, and why"). Known functional gaps remain on Windows and Linux.

## What this round changed

| Change | Detail | Evidence |
|---|---|---|
| Profile deletion fixed on Windows | Deleting a profile failed on Windows with "Neo could not remove this profile's local data" (WinError 32 on `neo.db`). The integrations, rules and skills stores opened connections with `with _connect() as conn`, which commits but never closes; since Python 3.11 such a connection stays open until the garbage collector runs. Windows cannot delete an open file; macOS and Linux can, which hid the bug. Each `_connect()` now commits or rolls back and then closes. | Before: 7 errors and 1 to 2 failures in every Windows backend job (CI run 41495897260 on `3908169`). After: 22/22 in all six Windows jobs (run 41521605136 on `a5dcf57`). A new regression test fails on `3908169` on every OS and passes on `a5dcf57`. |
| Tests moved out of the repository | All tests, fixtures, the Playwright smoke and the benchmarks now live in the private test repository and run against an exported copy of one exact commit. The pytest configuration, the `npm test` script and the Playwright dependency were removed from Neo. | Before removal, a fresh clone of the test repository on a clean machine profile collected exactly the same 21 backend, 997 frontend and 3 end-to-end tests as the in-repository suite, all passing. |

## Results on `a5dcf57`

| Where | Platform and runtimes | Suites | Result |
|---|---|---|---|
| Neo CI, [run 41521223289](https://github.com/chwla/neo/actions/runs/41521223289) | macOS, Linux and Windows hosted runners | Lint, startup, frontend build, Docker image smoke | 8 of 8 passed |
| Test CI, [run 41521605136](https://github.com/chwla/neo-tests/actions/runs/41521605136) | Ubuntu 24.04 x64 and arm64: Python 3.12.15, 3.13.17, 3.14.9 | Backend | 22/22 in each of 6 jobs |
| Same run | Windows Server 2025 x64 and Windows 11 Enterprise 25H2 arm64 (x64 Python): Python 3.12.10, 3.13.17, 3.14.10 | Backend | 22/22 in each of 6 jobs |
| Same run | Same four runner types: Node 22.23.4, 24.22, 26.13.0 | Frontend unit tests and production build | 997/997 in each of 12 jobs |
| Same run | Ubuntu 24.04 x64 | Browser smoke, Playwright Chromium, Firefox, WebKit engines | 3/3 |
| Same run (diagnostic, never gates) | Windows 11 arm64, native ARM64 Python 3.14.9 | `pip install` of the pinned requirements | Fails: `cryptography==50.0.0` publishes no win_arm64 wheel. Windows on ARM is supported only through x64 Python. |
| This Mac | macOS 26.6.2, Apple M5, arm64: Python 3.12.13, 3.13.15, 3.14.6; Node 22.23.4, 24.22.2, 26.4.0 | Backend, frontend, browser smoke | 22/22 per Python, 997/997 per Node, 3/3 |
| Linux containers (Docker Desktop on this Mac, repository copied into each container, unprivileged user) | `python:3.12-slim` and `python:3.14-slim` (Debian 13), arm64 and emulated amd64; `node:22-bookworm-slim` (Debian 12), arm64 and amd64 | Backend, frontend | 22/22 in each Python cell, 997/997 in each Node cell |

Commands, from a clone of the test repository:

```bash
python scripts/neo_tests.py all --neo <neo checkout> --ref a5dcf5735becd866d16c179a13a3591ec2a205f0 \
    --python python3.12 --python python3.13 --python python3.14 --expect-dir expectations
scripts/run_containers.sh --neo <neo checkout> --ref a5dcf5735becd866d16c179a13a3591ec2a205f0
```

CI runs only when started by hand with an explicit commit (workflow "Release tests", input
`neo_ref`). Every job checks that it tested that exact commit and that the commit is on
`main`. The suites fail on any failed test, an empty or changed set of collected tests, a skip
without a registered platform reason, or (Python 3.13 and later) a SQLite connection left
unclosed.

## Release gates

| Gate | Requirement | Status on `a5dcf57` |
|---|---|---|
| Completeness | Every shipping feature passes in every required platform and browser cell | NOT RUN: the feature coverage manifest, feature tests, real-browser journeys and real local-model checks are not built yet |
| Core functionality | Chat, streaming, cancellation, profiles, notes, files, agent approvals and tools, memory, settings | NOT RUN beyond the existing suites; known defect: Stop does not cancel the model call |
| Security | No approval bypass, unsafe permissions, cross-profile access, egress or key-policy bypass | NOT RUN beyond the existing suites; earlier security regression tests were lost and are not yet replaced |
| Data integrity | No data loss, corruption or failed upgrade | NOT RUN beyond the existing suites |
| Platform parity | The same features on Windows, Linux and macOS | FAIL: undo, patch apply, delivery and checkpoint restore are refused on Windows; external coding agents are disabled on Linux, Windows and Docker; commands and tests need per-run approval outside macOS |
| Test integrity | Guards against missing tests, empty runs and unexplained skips | PASS for the existing suites |
| Commit identity | All evidence on the release commit | PASS for the results above (`a5dcf57`) |
| Packaging | Clean installs on every supported runtime | PASS for Linux and Windows x64 runners and macOS; native ARM64 Python on Windows is not supported (see above) |

## Not run, and why

- **Feature coverage of the whole product:** the existing suites test the profile, session,
  deletion and compare fixes and the frontend's pure modules. Most shipping features (chat
  streaming, agent mode, tools, files, notes encryption in the browser, memory, search,
  calendar, voice, gallery, connectors, settings) have no end-to-end coverage yet. Building it
  is in progress.
- **Real browsers:** only Playwright's bundled engines have run. Real Chrome, Edge, Firefox
  and Safari acceptance has not run on any platform. Desktop Safari needs
  `safaridriver --enable` on the test Mac.
- **Windows 11 x64 desktop and Linux x64 desktop:** no such machines are available to the test
  setup yet. The hosted runners are Windows Server 2025, a Windows 11 Enterprise arm64 image
  and Ubuntu server images with a virtual display.
- **Interactive Windows 11 arm64 and Ubuntu desktop VMs:** not set up yet.
- **Browser floor (Chrome and Edge 111, Firefox 121, Safari 16.2):** not run. Chrome and Edge
  111 have no official installers; Safari 16.2 needs a macOS 13.1 guest, which Apple
  silicon M4-class Macs are reported unable to boot.
- **iOS Safari (16.2 and current):** not run. Full Xcode and the iOS Simulator are not
  installed on the test Mac, and no iOS device is available. Neo also ships no web app
  manifest, so native notifications on iOS (Home Screen web apps only) are unavailable.
- **Real local models on each OS, connected Google accounts, the GitHub connector and external
  coding CLIs:** not run.

## Known defects and remaining risks

- **Stop does not cancel the model call.** Stopping a chat reply stops the reply, but the model
  call keeps generating on its own thread until it finishes. Profile deletion cancels these
  calls; the Stop button does not.
- **Windows:** undo, patch apply, delivery and checkpoint restore are refused, because Neo
  cannot yet guarantee there that a restoring write stays inside the folder. A race-free
  Windows implementation is designed but not built.
- **Linux, Windows and Docker have no OS sandbox.** External coding agents are disabled there,
  and commands and tests run with the full user account once a person approves each run.
- **Provider keys from the environment:** any non-guest profile can still spend an operator key
  at its own provider, and any process running as the same OS user can read the server
  environment.
- **Deleting a profile mid-call:** deletion waits up to 10 s for the profile's workers; a worker
  still blocked on a provider after that ends at its next write.
- **Connector key rotation** (re-sealing under a new key) is not supported.
- **The Notes chunk** is about 707 kB.

## History: fixes from the 2026-10-30 and 2026-10-31 rounds

These fixes remain in the code. The regression tests that accompanied them were kept in
local test folders that no longer exist; replacing that coverage in the test repository is
part of the work still to do.

| ID | Finding | Fix |
|---|---|---|
| B1 | The test suite was deleted; the frontend failed 2 of 980 | New local-only suite in `tests/`, with the crypto fixture restored byte-identical. Because `tests/` is not in the repository, the frontend's `notes-crypto` and `no-em-dashes` tests fail in any checkout without it; CI therefore does not run the frontend tests. `conftest.py` sandboxes data before any app import, and a SQLite guard fails any test that opens a database outside the temp directory. The crypto fixture is restored byte-identical. |
| B2 | Anonymous `/api/health/ready` ran a web search and a model call, and leaked the data path | Anonymous callers get status only, from storage checks. Signed-in callers get the full report, cached for 30 s. Probes are cached for 10 s. |
| B3 | Interrupted agent runs were never failed in profile databases | Recovery runs per profile, for accounts and guests, and keeps the stale-heartbeat guard |
| B4, X1 | Commands and tests ran unsandboxed: on macOS when the probe failed, and on Windows/Linux in AUTO mode with no prompt | One rule in `app/services/isolation/launch.py`, called before every command-sandbox and test-runner launch: sandboxed, or unsandboxed only with a person's approval of that run after being shown the exposure, or refused. A failing sandbox is never a fallback. The agent cannot approve itself. AUTO asks. |
| P3 | `isolation.status()` probed on every call | Successes are cached for 5 minutes; failures are re-probed |
| B5 | A repository's signature settings could run a program on `git log` | `gpg.program`, `gpg.ssh.program` and `gpg.x509.program` point at the null device; `log.showSignature` is off; signature flags and formats are refused in the sandbox |
| B6 | Startup crashed if run records existed but isolation did not | Recovery is deferred with a warning |
| W1 | `safe_fs` raised `AttributeError` on Windows, silently disabling undo | Fails closed with a 400 and an explanation. The journal records touched files as "snapshot unavailable"; the run panel says why undo is missing; AUTO asks before such edits. |
| W2 | The vault key could never be read back on Windows | The key gets an access list restricted to your account. It is verified via SDDL, so the check works in any display language. POSIX keeps the 0600 check. |
| W3 | Deletion failed on read-only files (git objects) | `remove_tree()` in `app/core/paths.py`, used at all 23 call sites; ignored failures are logged |
| D4 | The Docker master key was never provisioned | One path: generated on first run, beside the data. Never silently replaced. Reset is deliberate and destructive, and documented in the README under "Connector master key". |
| P2 | A Files upload could be deleted by the Notes garbage collector | Deduplication only within the same source type |
| M1 | Any session could send an operator's `*_API_KEY` anywhere | Keys go only over https to their provider's own host; no IP literals, userinfo, other ports or redirects; never for guests; Neo's search keys refused |
| M2 | No request-body limits | Outermost ASGI limiter, on both declared length and streamed bytes, with per-route caps |
| BR4 | The documented `npm run dev` flow got 403 on every write | The dev proxy rewrites Origin only for the dev server's own page |
| BR3 | Copy buttons could throw or fail silently | `src/clipboard.js` with a fallback; failures are shown |
| H1 | No clickjacking, `nosniff` or referrer headers | Added to every response; CSP adds `frame-ancestors 'none'` |
| H2 | Cookies were never `Secure` | `NEO_COOKIE_SECURE` |
| H3 | A 201 to 256 character password could not be changed | One 256-character ceiling |
| H4 | Timeout output was lost | TimeoutExpired bytes are decoded |
| H5 | `read_file` loaded whole files into memory | Bounded read, cut on a character boundary |
| H8 | The venv lagged the pins | Synced; `pip-audit` reports no known vulnerabilities |
| D1, D2, D5 | Docker configuration | `extra_hosts` added to compose; `.dockerignore` uses `**/`; broad-folder and readiness docs corrected |
| Found during testing | The chat event stream crashed after its profile was deleted | Queue readers tolerate a deleted database, and the stream ends when its profile is gone |
| P5 | Deleting a profile left its work running, and a late worker recreated the deleted directory | Before storage is removed, deletion (account or guest) cancels the profile's queued and running chat turns, agent runs (queued, running, waiting for approval), research jobs and streaming model calls, then waits up to 10 s for this process's workers to stop. External CLI runs stop through their existing cancel probe. `database_url_for()` and `profile_database()` no longer create directories; a deleted profile is marked so its storage is never initialised or created again in this process. A failed delete keeps the account usable. An agent worker that loses its database still frees its turn slot. |
| P4 | Every authenticated request wrote the registry | `last_seen_at` is written at most once per 5 minutes. Absolute expiry is unchanged; idle expiry can come up to 5 minutes early, never late. `_registry_path()` does its directory and permission checks once per registry, and only after the file exists and is closed off. |
| P1 | Model compare worker threads lost the profile, so they read the base database and a guest's run was not held to the guest key policy | The route pulls events through `asyncio.to_thread`, and the runner submits each worker in a copy of the caller's context. The thread pools in `research/retrieval.py` and `search/content.py` were checked and left alone: they only make network calls through egress, and read settings in constructors on the calling thread. |
| H6 | Dead Qdrant archive module and dependency | `app/services/archives.py` deleted; `qdrant-client` removed from `pyproject.toml`; `uv.lock` and `requirements.txt` regenerated, which also drops grpcio, h2, hpack, hyperframe, portalocker, pywin32, httpx, httpcore and protobuf from the runtime install. The `qdrant_url` setting stays, because `isolation/network.py` uses it to keep a legacy local Qdrant blocked from sandboxed processes. |
| H7 | A GitHub token value switched production code to fixture data | The fixture path is removed; nothing but the deleted test suite used it. The `noqa: S307` comment in `model_compare/generators.py` moved to the `eval(` line, where ruff reports it. |
| H9 | Stray runtime files from runs in the wrong directory | Deleted, as you approved: `:memory:.ses`; `neo_memory.db`, `-shm`, `-wal` and `neo_llms.json` in `app/`; `neo_memory.db` and `neo_llms.json` in `frontend/`; `frontend/profiles/` (0 profiles); and the root `neo_memory.db`, `-shm` and `-wal` (61 chats from 2026-07-05 to 07-12 that no profile could see). Each was confirmed untracked first. No Neo process had the root database open; Docker Desktop's file-sharing service held a read-only handle on its `-shm`. A local start recreates an empty root database. |
