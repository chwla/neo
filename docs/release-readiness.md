# Release readiness

Last updated 2026-11-02, after commit `dcfb6f3` and its first CI run.

## Verdict: not ready

The security boundary and data-integrity blockers found in the 2026-10-30 audit are fixed, and each fix has a regression test. The Docker image now builds and passes its smoke test on Docker Desktop. Neo is still not ready to release for everyone.

- **Platforms:** native Windows and Linux have not been run at all, and hosted CI has not run.
- **Windows features:** agent undo, patch apply, delivery and checkpoint restore are refused rather than made safe there.

| Target | Status | Evidence |
|---|---|---|
| macOS, native | Verified locally | Backend suite on Python 3.14 and 3.12; browser smoke in three engines; live checks against a restarted backend |
| Windows, native | **Unverified**; four features unsupported | Needs the `Backend lint and startup (windows-latest, ...)` CI jobs to pass. Undo, patch apply, delivery and checkpoint restore fail closed. |
| Linux, native | **Unverified** | Needs the `Backend lint and startup (ubuntu-latest, ...)` CI jobs to pass |
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

**Deviation from the plan.** Rows sealed under a replaced connector key are detected when they fail to open: the connection is marked `reconnect_required` and never returns a 500. The plan called for a per-row `key_fingerprint` column. Without it, the message cannot distinguish "key replaced" from "record damaged".

## Evidence

Run on macOS 26 (Darwin 25.6). Every result below was observed.

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

## Not run, and why

- **Hosted CI, first run on `dcfb6f3`:** the Docker job passed. Every other job failed only because it expected the test files kept out of the repository (`ruff check app tests`, the root crypto fixture, the Playwright config). The workflow now lints `app` and starts Neo on each OS and Python version, the frontend job checks the production build only, and the browser job is gone: none of them needs a test file. Those jobs need the next push to run; until they pass, Windows and Linux stay unverified.
- **Docker on Linux Engine:** the image was verified only on Docker Desktop (macOS), which defines `host.docker.internal` itself; the compose `extra_hosts` mapping matters on Linux Engine and needs the `docker` CI job.
- **Real Windows ACL and Windows git:** they exist only in the Windows CI job. Locally the Windows key path is exercised with a mocked `icacls`.
- **Safari and iOS Safari:** no device or automation available. WebKit is the closest evidence.

## Remaining risks

**Known gaps:**
- **Windows `safe_fs`:** a race-free implementation (`NtCreateFile` relative to a directory handle) would restore undo, patch apply, delivery and checkpoint restore on Windows. It is not built, because it cannot be verified without Windows CI.
- **Linux and Windows have no OS sandbox.** Commands and tests there run with the full user account once approved. The approval is what protects the user.
- **Hardened but outside the launch rule:** language servers run fixed binaries. Git runs with the repository's program-running settings overridden.
- **Provider keys from the environment:** any non-guest profile can still spend an operator key at its own provider, and any process running as the same OS user can read the server environment.

**Not addressed in this pass:**
- **P1:** model compare drops the profile context in its worker threads. It is latent today.
- **P4:** a session write on every authenticated request.
- **P5:** deleting a profile does not cancel its in-flight work; only the stream crash it caused is fixed.
- **H6, H7, H10:** dead `qdrant-client` module, a test token in GitHub client code, the 715 kB Notes chunk.
- **H9:** stray runtime files. Awaiting your decision, including whether to export the 61 legacy chats in the root `neo_memory.db`.

**Operational:**
- **Connector key rotation** (re-sealing under a new key) is not supported.
- **Your Docker setup.** The `neo` container is recreated from compose on the new image and now binds `127.0.0.1:8000` only. Before this it published `0.0.0.0`, reachable from your network. Still present and left untouched: an orphaned `neo-searxng` container from the old compose (no longer used, since SearXNG now runs inside Neo), an exited `neo-prev-20260821-211451`, and older `neo:rollback-*` images.
- **The local dev backend is stopped,** so the container can serve `127.0.0.1:8000`. To run the dev backend again, stop the container or use another port.
- **The connector key on this machine** was generated on first use during the live checks, at `profiles/.neo-connector-master-key`. There were no sealed records before. Back it up with your data.

## Gates still to clear

1. Commit the updated workflow and docs (no test files) and push, then get every job green: backend lint and startup on three OSes × two Pythons, and the frontend build. Docker on Linux Engine has already passed.
2. Decide on H9.
3. Decide whether the Windows feature gap is acceptable for the release, or build and CI-verify the Windows `safe_fs` backend.
