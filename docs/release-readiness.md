# Release readiness

Last updated 2026-11-02. All 8 CI checks passed on commit `17c63fc`. The P1, P4, P5, H6, H7 and H9 fixes below come after that commit: verified locally, not yet committed or run through CI.

## Verdict: not ready

The security boundary and data-integrity blockers found in the 2026-10-30 audit are fixed, and each fix has a regression test. The Docker image now builds and passes its smoke test on Docker Desktop. Neo is still not ready to release for everyone.

- **Platforms:** native Windows and Linux have not been run at all, and hosted CI has not run.
- **Windows features:** agent undo, patch apply, delivery and checkpoint restore are refused rather than made safe there.

| Target | Status | Evidence |
|---|---|---|
| macOS, native | Verified locally | Backend suite on Python 3.14 and 3.12; browser smoke in three engines; live checks against a restarted backend |
| Windows, native | Installs, lints, starts and serves (CI); features beyond that **unverified**; four features unsupported | CI on `17c63fc`, Python 3.12 and 3.14: startup, anonymous readiness, sign-up, connector key created with a restricted access list, clean log. The test suite and real use (agent tools, files, notes) have never run on Windows. Undo, patch apply, delivery and checkpoint restore fail closed. |
| Linux, native | Installs, lints, starts and serves (CI); features beyond that **unverified** | CI on `17c63fc`, Python 3.12 and 3.14, same checks as Windows. The test suite runs only locally (macOS). |
| Docker image | Verified on Linux Docker Engine (CI) and Docker Desktop (macOS) | CI job "Docker image build and smoke" passed on `dcfb6f3`: builds, no runtime data in the image, starts, health, sign-up, connector key, key kept across a restart. Also smoke-tested locally: see Evidence. |
| Chromium engine (Chrome, Edge) | Verified locally (macOS) | Playwright smoke, Chromium 156 headless shell. The Playwright files are kept local, so CI has no browser job. |
| Firefox engine | Verified locally (macOS) | Playwright smoke, Firefox 157 |
| WebKit engine | Verified locally (macOS) | Playwright smoke, WebKit 27.2. This is an engine check, not Safari itself. |
| Safari, iOS Safari | **Unverified** | Not tested; WebKit is the closest evidence |

The supported browser floor is Chrome/Edge 111, Firefox 121 and Safari/iOS 16.2. It is declared in `frontend/package.json` (`browserslist`) and `frontend/vite.config.js` (`BUILD_TARGET`), and `frontend/tests/browser-baseline.test.mjs` keeps the two in step.

## What changed

Every fix has a regression test. Test files are kept out of the repository by choice: the backend suite in `tests/` and the new frontend test files exist only locally, the test names below refer to those local files, and CI runs lint, startup and build checks instead.

| ID | Finding | Fix | Test |
|---|---|---|---|
| B1 | The test suite was deleted; the frontend failed 2 of 980 | New local-only suite in `tests/`, with the crypto fixture restored byte-identical. Because `tests/` is not in the repository, the frontend's `notes-crypto` and `no-em-dashes` tests fail in any checkout without it; CI therefore does not run the frontend tests. `conftest.py` sandboxes data before any app import, and a SQLite guard fails any test that opens a database outside the temp directory. The crypto fixture is restored byte-identical. | `tests/test_harness_isolation.py` |
| B2 | Anonymous `/api/health/ready` ran a web search and a model call, and leaked the data path | Anonymous callers get status only, from storage checks. Signed-in callers get the full report, cached for 30 s. Probes are cached for 10 s. | `tests/test_health_ready.py` |
| B3 | Interrupted agent runs were never failed in profile databases | Recovery runs per profile, for accounts and guests, and keeps the stale-heartbeat guard | `tests/test_profile_recovery.py` |
| B4, X1 | Commands and tests ran unsandboxed: on macOS when the probe failed, and on Windows/Linux in AUTO mode with no prompt | One rule in `app/services/isolation/launch.py`, called before every command-sandbox and test-runner launch: sandboxed, or unsandboxed only with a person's approval of that run after being shown the exposure, or refused. A failing sandbox is never a fallback. The agent cannot approve itself. AUTO asks. | `tests/test_isolation_launch.py` |
| P3 | `isolation.status()` probed on every call | Successes are cached for 5 minutes; failures are re-probed | `tests/test_isolation_launch.py` |
| B5 | A repository's signature settings could run a program on `git log` | `gpg.program`, `gpg.ssh.program` and `gpg.x509.program` point at the null device; `log.showSignature` is off; signature flags and formats are refused in the sandbox | `tests/test_git_signature_hardening.py` (proves the planted program runs under plain git first) |
| B6 | Startup crashed if run records existed but isolation did not | Recovery is deferred with a warning | `tests/test_startup_isolation_unavailable.py` |
| W1 | `safe_fs` raised `AttributeError` on Windows, silently disabling undo | Fails closed with a 400 and an explanation. The journal records touched files as "snapshot unavailable"; the run panel says why undo is missing; AUTO asks before such edits. | `tests/test_safe_fs_platform.py` |
| W2 | The vault key could never be read back on Windows | The key gets an access list restricted to your account. It is verified via SDDL, so the check works in any display language. POSIX keeps the 0600 check. | `tests/test_vault_key.py` (Windows path mocked) |
| W3 | Deletion failed on read-only files (git objects) | `remove_tree()` in `app/core/paths.py`, used at all 23 call sites; ignored failures are logged | `tests/test_remove_tree.py` |
| D4 | The Docker master key was never provisioned | One path: generated on first run, beside the data. Never silently replaced. Reset is deliberate and destructive, and documented in the README under "Connector master key". | `tests/test_vault_key.py` |
| P2 | A Files upload could be deleted by the Notes garbage collector | Deduplication only within the same source type | `tests/test_files_dedup.py` |
| M1 | Any session could send an operator's `*_API_KEY` anywhere | Keys go only over https to their provider's own host; no IP literals, userinfo, other ports or redirects; never for guests; Neo's search keys refused | `tests/test_provider_key_policy.py` |
| M2 | No request-body limits | Outermost ASGI limiter, on both declared length and streamed bytes, with per-route caps | `tests/test_body_limits.py` |
| BR4 | The documented `npm run dev` flow got 403 on every write | The dev proxy rewrites Origin only for the dev server's own page | `frontend/tests/dev-proxy-origin.test.mjs`, plus a live check |
| BR3 | Copy buttons could throw or fail silently | `src/clipboard.js` with a fallback; failures are shown | `frontend/tests/clipboard.test.mjs` |
| H1 | No clickjacking, `nosniff` or referrer headers | Added to every response; CSP adds `frame-ancestors 'none'` | `tests/test_security_headers.py` |
| H2 | Cookies were never `Secure` | `NEO_COOKIE_SECURE` | `tests/test_security_headers.py` |
| H3 | A 201 to 256 character password could not be changed | One 256-character ceiling | `tests/test_security_headers.py` |
| H4 | Timeout output was lost | TimeoutExpired bytes are decoded | `tests/test_isolation_launch.py` |
| H5 | `read_file` loaded whole files into memory | Bounded read, cut on a character boundary | `tests/test_security_headers.py` |
| H8 | The venv lagged the pins | Synced; `pip-audit` reports no known vulnerabilities | Command below |
| D1, D2, D5 | Docker configuration | `extra_hosts` added to compose; `.dockerignore` uses `**/`; broad-folder and readiness docs corrected | Review; the `docker` CI job |
| Found during testing | The chat event stream crashed after its profile was deleted | Queue readers tolerate a deleted database, and the stream ends when its profile is gone | `tests/test_profile_recovery.py`; browser smoke ran 3 times with no traceback |
| P5 | Deleting a profile left its work running, and a late worker recreated the deleted directory | Before storage is removed, deletion (account or guest) cancels the profile's queued and running chat turns, agent runs (queued, running, waiting for approval), research jobs and streaming model calls, then waits up to 10 s for this process's workers to stop. External CLI runs stop through their existing cancel probe. `database_url_for()` and `profile_database()` no longer create directories; a deleted profile is marked so its storage is never initialised or created again in this process. A failed delete keeps the account usable. An agent worker that loses its database still frees its turn slot. | `tests/test_profile_delete_stops_work.py`; live check below |
| P4 | Every authenticated request wrote the registry | `last_seen_at` is written at most once per 5 minutes. Absolute expiry is unchanged; idle expiry can come up to 5 minutes early, never late. `_registry_path()` does its directory and permission checks once per registry, and only after the file exists and is closed off. | `tests/test_session_touch_throttle.py`; live check below |
| P1 | Model compare worker threads lost the profile, so they read the base database and a guest's run was not held to the guest key policy | The route pulls events through `asyncio.to_thread`, and the runner submits each worker in a copy of the caller's context. The thread pools in `research/retrieval.py` and `search/content.py` were checked and left alone: they only make network calls through egress, and read settings in constructors on the calling thread. | `tests/test_profile_context_threads.py` |
| H6 | Dead Qdrant archive module and dependency | `app/services/archives.py` deleted; `qdrant-client` removed from `pyproject.toml`; `uv.lock` and `requirements.txt` regenerated, which also drops grpcio, h2, hpack, hyperframe, portalocker, pywin32, httpx, httpcore and protobuf from the runtime install. The `qdrant_url` setting stays, because `isolation/network.py` uses it to keep a legacy local Qdrant blocked from sandboxed processes. | `tests/test_cleanup_h6_h7.py`; clean Python 3.12 install and Docker build below |
| H7 | A GitHub token value switched production code to fixture data | The fixture path is removed; nothing but the deleted test suite used it. The `noqa: S307` comment in `model_compare/generators.py` moved to the `eval(` line, where ruff reports it. | `tests/test_cleanup_h6_h7.py` |
| H9 | Stray runtime files from runs in the wrong directory | Deleted, as you approved: `:memory:.ses`; `neo_memory.db`, `-shm`, `-wal` and `neo_llms.json` in `app/`; `neo_memory.db` and `neo_llms.json` in `frontend/`; `frontend/profiles/` (0 profiles); and the root `neo_memory.db`, `-shm` and `-wal` (61 chats from 2026-07-05 to 07-12 that no profile could see). Each was confirmed untracked first. No Neo process had the root database open; Docker Desktop's file-sharing service held a read-only handle on its `-shm`. A local start recreates an empty root database. | `git ls-files`, `lsof`; see "H9 deletion" under Evidence |

The test files named in the rows above P5 belonged to the earlier local suite, which has since been deleted; they no longer exist. The P5 to H9 rows name the current local suite: 21 tests in `tests/`, including `tests/test_harness.py`, which checks that the suite runs on throwaway data only.

**Deviation from the plan.** Rows sealed under a replaced connector key are detected when they fail to open: the connection is marked `reconnect_required` and never returns a 500. The plan called for a per-row `key_fingerprint` column. Without it, the message cannot distinguish "key replaced" from "record damaged".

## Evidence

Run on macOS 26 (Darwin 25.6). Every result below was observed. The first table is from the round that ended at `17c63fc`; its backend test counts refer to the earlier local suite, which has since been deleted. The second table is the P1, P4, P5, H6, H7 and H9 round.

| Check | Command | Result |
|---|---|---|
| Backend tests, Python 3.14.6 | `.venv/bin/python -m pytest -q -p no:cacheprovider tests` | 129 passed |
| Backend tests, Python 3.12.13 | Same, in a scratch venv built from `requirements.txt` | 129 passed |
| Lint | `.venv/bin/ruff check app tests` | All checks passed |
| Frontend tests | `cd frontend && npm test` | 997 passed, 0 failed |
| Production build | `npx vite build` | Built. One existing warning: the Notes chunk is about 715 kB. |
| Browser smoke | `npx playwright test` | Chromium, Firefox and WebKit passed, 3 consecutive runs, no server traceback |
| Python dependencies | `uvx --from pip-audit pip-audit -r requirements.txt` | No known vulnerabilities |
| Frontend dependencies | `npm audit` | 0 vulnerabilities |
| Live: anonymous readiness | `curl /api/health/ready` | `{"status":"ready"}`; 5 calls added 0 provider-request rows (2164 before and after) |
| Live: signed-in readiness | Same, signed in | Full checks. Search reports not ready because of this network's known DuckDuckGo/Bing blocking, which predates these changes. |
| Live: restart recovery | Stale and fresh `running` rows, then a restart | Stale became `failed`; fresh stayed `running` |
| Live: upload limit | 7 MB upload | 413 |
| Live: dev proxy | POST through Vite | Dev origin passed (405); foreign origin 403; direct 403 |
| Live: headers | `curl -D -` | `nosniff`, `DENY`, `no-referrer` present |
| Docker build | `docker build -t neo:latest .` (BuildKit) | Built `e355fd476ffa`, Python 3.12.16, runs as uid 10001 |
| Docker: no runtime data | `find /app/app -name '*.db*' -o -name neo_llms.json -o -name profiles -o -name __pycache__` | Nothing found |
| Docker smoke, fresh volume | `neo-smoke` on 127.0.0.1:18000 | Health live; anonymous readiness status-only; 3 headers; sign-up 201; connector key generated (0600; under the old image this setup refused); model reachable via `host.docker.internal`; 7 MB upload 413 |
| Docker: restart persistence | `docker restart neo-smoke` | Key unchanged and still 0600; profile and session survived; 0 errors in the log |
| Docker: your container | `docker compose up -d` | `neo` recreated on the new image with its existing data volume (6 profiles kept, existing July key in place), bound to `127.0.0.1:8000` only, healthy, 0 errors. The old image is removed. |

The backend was restarted with the documented clean-environment command before the live checks. Disposable profiles were deleted afterwards.

### P1, P4, P5, H6, H7 and H9 round

| Check | Command | Result |
|---|---|---|
| Lint | `.venv/bin/ruff check app`, then `.venv/bin/ruff check tests` | All checks passed, both |
| Backend tests, Python 3.14.6 | `.venv/bin/python -m pytest -q -p no:cacheprovider tests` | 21 passed, 1 warning (Starlette's own TestClient deprecation notice) |
| Backend tests, Python 3.12.13 | Same, in a scratch venv built from the new `requirements.txt`, plus pytest, httpx and ruff | 21 passed, 2 warnings (both from third-party packages) |
| The tests catch the old behaviour | Each fix undone in turn by a throwaway pytest plugin | Every one fails tests: no cancellation (2 failed), directories created again (2), model calls not cancelled (2), a write on every request (2), registry checks not memoised (1), runner context dropped (both P1 tests), route context dropped (1) |
| Frontend build | `cd frontend && npx vite build --outDir <scratch dir>` | Built. Existing warning: the Notes chunk is 706.62 kB (H10). |
| No em dashes | `node --test tests/no-em-dashes.test.mjs`, in `frontend/` | 4 passed |
| S307 placement | `ruff check --isolated --select S307` on `generators.py` | The committed version: 1 error. Now: all checks passed. |
| Dependency lock | `uv lock`, then the export command in the `requirements.txt` header | The lock lost grpcio, h2, hpack, hyperframe, portalocker, pywin32 and qdrant-client; no other version changed. `requirements.txt` also no longer lists httpx, httpcore and protobuf, which were runtime dependencies only through qdrant-client. |
| Clean install | Python 3.12.13 scratch venv from `requirements.txt` | `qdrant_client` and `httpx` not installed. Neo started on 127.0.0.1:18081: health ok, sign-up 201, delete 204, no errors in the log. |
| Docker build | `docker build -t neo-verify:h6 .` (BuildKit) | Built `6d05d864d53a`, 761 MB. `neo:latest` and your `neo` container were not touched. |
| Docker smoke | `docker run --rm` of that image on 127.0.0.1:18002 | `qdrant_client` absent; `app.main` imports; health ok; anonymous readiness `{"status":"ready"}`; sign-up, then delete 204, no accounts left; no errors in the log. The container and its volume are gone. |
| Live: P5 | A separate backend on 127.0.0.1:18080, clean environment, temporary data and working directories | An account and a guest, each deleted while llama3.2:3b was streaming a reply: 204 in 0.23 s and 0.12 s. The open event stream received `run.cancelled` and ended 1.07 s after the delete. Both directories were gone and stayed gone through 40 s of checks every 0.25 s, longer than the 30 s calendar sweep. 0 tracebacks or warnings in the backend log. |
| Live: P4 | Same backend, 20 authenticated requests over 3.3 s | `registry.db` modification time and size unchanged; no `-wal` file created |
| H9 deletion | `git ls-files` for each path; `lsof` | All 13 files and `frontend/profiles/` were untracked. Nothing had the root database open except Docker Desktop's read-only file-sharing handle on its `-shm`; the `neo` container had none of these files open. |

A first live run, made before model calls were cancelled, logged two tracebacks from model-call threads writing to the deleted database (the directories still stayed gone). The model-call cancellation in P5 came from that run. Every disposable profile was deleted, and both temporary backends were stopped.

## Not run, and why

- **Hosted CI:** the first run (`dcfb6f3`) failed only where jobs expected test files kept out of the repository. The workflow was changed to need none, and on `17c63fc` all 8 checks passed: backend lint and startup on macOS, Linux and Windows on Python 3.12 and 3.14, the frontend production build, and the Docker smoke test. CI does not run any test suite, so on Linux and Windows nothing beyond startup has been exercised.
- **Docker on Linux Engine:** the image was verified only on Docker Desktop (macOS), which defines `host.docker.internal` itself; the compose `extra_hosts` mapping matters on Linux Engine and needs the `docker` CI job.
- **Real Windows ACL and Windows git:** they exist only in the Windows CI job. Locally the Windows key path is exercised with a mocked `icacls`.
- **Safari and iOS Safari:** no device or automation available. WebKit is the closest evidence.
- **This round in CI:** not run. It runs after the next push.
- **Frontend unit tests (`npm test`), this round:** not run. `notes-crypto.test.mjs` reads `tests/fixtures/notes_key_vectors.json`, which was deleted with the earlier local suite, so that test fails until the fixture is restored.
- **Model compare, live:** not run. P1 is covered by tests of the route and the runner.

## Remaining risks

**Known gaps:**
- **Windows `safe_fs`:** a race-free implementation (`NtCreateFile` relative to a directory handle) would restore undo, patch apply, delivery and checkpoint restore on Windows. It is not built, because it cannot be verified without Windows CI.
- **Linux and Windows have no OS sandbox.** Commands and tests there run with the full user account once approved. The approval is what protects the user.
- **Hardened but outside the launch rule:** language servers run fixed binaries. Git runs with the repository's program-running settings overridden.
- **Provider keys from the environment:** any non-guest profile can still spend an operator key at its own provider, and any process running as the same OS user can read the server environment.
- **Deleting a profile mid-call (P5):** deletion waits up to 10 s for the profile's workers. A worker still blocked on a provider after that is not waited for: it ends at its next write, which fails with a logged traceback. Its directory is not recreated.

**Found during testing, not fixed:**
- **Stop does not cancel the model call.** Stopping a chat reply stops the reply, but the model call runs on its own thread and keeps generating until it finishes. Only an explicit `/provider-runtime` cancel stops it. Profile deletion now cancels these calls; the Stop button does not.

**Not addressed in this pass:**
- **H10:** the Notes chunk, now 706.62 kB.

**Operational:**
- **Connector key rotation** (re-sealing under a new key) is not supported.
- **Your Docker setup.** The `neo` container is recreated from compose on the new image and now binds `127.0.0.1:8000` only. Before this it published `0.0.0.0`, reachable from your network. Still present and left untouched: an orphaned `neo-searxng` container from the old compose (no longer used, since SearXNG now runs inside Neo), an exited `neo-prev-20260821-211451`, and older `neo:rollback-*` images.
- **The local dev backend is stopped,** so the container can serve `127.0.0.1:8000`. To run the dev backend again, stop the container or use another port.
- **The connector key on this machine** was generated on first use during the live checks, at `profiles/.neo-connector-master-key`. There were no sealed records before. Back it up with your data.

## Gates still to clear

1. Done: CI is green on `17c63fc`.
2. Done: H9 decided and the stray files deleted.
3. Decide whether the Windows feature gap is acceptable for the release, or build and CI-verify the Windows `safe_fs` backend.
4. Commit the P1, P4, P5, H6, H7 and H9 round (no test files) and get CI green on it.
