# Neo memory v2 Phase 4 manual validation

Phase 4 is the first phase where testing natural-language memory extraction is meaningful. It
turns user-authored text into typed, grounded proposals and, when policy permits, routes them
through the Phase 3 adapters and Phase 2 mutation kernel.

This procedure does **not** test normal Neo recall, direct answers, prompt serialization or
injection, or plan generation. Those are Phase 5 responsibilities. A successful run proves only
the Phase 4 extraction, correction-planning, candidate, and mutation path.

## Safety boundary

Both modes create a fresh database below a temporary `neo-memory-v2-phase4-*` directory. The
script binds one explicit test owner to that disposable database and enables v2 only for that
owner. It never opens a normal profile database. Without `--keep`, the fixture database is
removed after a successful run; failures retain artifacts for diagnosis.

Do not paste real secrets or sensitive personal data into live mode. The policy rejects
prohibited content and uses the encrypted candidate/canonical path for explicitly requested
sensitive facts, but test data should remain synthetic.

Sensitive and prohibited fixture values are unique runtime sentinels. They are never printed.
Before emitting buffered fixture output, the validator scans serialized results, diagnostics,
stdout, and every retained artifact. Prohibited input must leave table counts unchanged. Approved
sensitive input must have encrypted record, candidate, operation, and provenance payloads with
null plaintext display columns.

## Deterministic fixture mode

Run:

```bash
.venv/bin/python scripts/manual_memory_v2_phase4.py --keep
```

Fixture mode validates deterministic behavior without a network call or live model. It covers
stable creation, the critical correction, category and domain handling, pure retraction,
additive and `not only` boundaries, transient/third-party/assistant rejection, malformed output,
timeout, sensitivity policy, candidate limits, explicit batch behavior, grounded ambiguous-review
persistence after invalid model schema,
sync/stream idempotency, and incognito zero-call behavior.

For every scenario the script prints the user source (redacted for sensitive/prohibited cases),
pre-parser result, model summary, grounding decisions, candidate decisions, current-turn
override, and bounded diagnostic. At the end it prints canonical records, review candidates,
supersession relations, source provenance, operations, outbox rows, and extraction diagnostics.
It exits nonzero if an invariant fails. Success ends with:

```text
phase4_fixture_validation=PASS
prohibited_plaintext_leak_count=0
sensitive_plaintext_log_leak_count=0
sensitive_plaintext_artifact_leak_count=0
sensitive_payload_encrypted=true
category_reconfirm_suppressed_ids=[]
ambiguous_conflict_suppression_authorized=false
```

The script exits nonzero and withholds its buffered scenario output if a sentinel occurs in a
forbidden output or artifact, if encryption proof fails, if category reconfirmation suppresses an
active record, or if an ambiguous conflict authorizes suppression.

The retained directory and a cleanup command are printed. Run that exact cleanup command after
inspection.

## Optional interactive live-model mode

Live mode evaluates a named provider protocol, not a generic JSON endpoint. `direct_json` means
the HTTP response body itself is the strict extraction schema. `ollama` means a non-streaming
Ollama `/api/chat` request and an outer response envelope whose only model output is
`message.content`. Provider selection is required; endpoint-name guessing is not used.

For the independently validated Ollama configuration, run exactly:

```bash
.venv/bin/python scripts/manual_memory_v2_phase4.py \
  --probe-live-model \
  --provider ollama \
  --endpoint 'http://127.0.0.1:11434/api/chat' \
  --model 'qwen3-coder:30b' \
  --model-timeout-seconds 120
```

The probe uses only the constant synthetic capability input and does not create a temporary
directory or database. It reads `/api/version` and `/api/tags`, warms the exact configured model,
then tests the complete production schema object, JSON format, `think`, `seed`, `num_predict`, and
`keep_alive` independently. A small toy schema is insufficient because it may pass while the
provider's grammar compiler rejects the bounded repetitions in the complete extraction schema.
The probe prints reachability, exact model availability, warm-up success/latency, server
version, each capability, the selected mode, and a bounded sanitized failure code/message. A
successful automatic probe prefers `ollama_schema` and selects `ollama_json` only when the schema
probe fails but JSON mode succeeds. It does not silently downgrade an extraction request after an
arbitrary failure.

After a successful probe, run exactly:

```bash
.venv/bin/python scripts/manual_memory_v2_phase4.py \
  --interactive \
  --live-model \
  --provider ollama \
  --endpoint 'http://127.0.0.1:11434/api/chat' \
  --model 'qwen3-coder:30b' \
  --model-timeout-seconds 120 \
  --ollama-request-mode auto \
  --confirm-disposable-live-model \
  --keep
```

For a direct extraction service, use `--provider direct_json`. `--token-env` names an optional
bearer-token environment variable. The script fails closed if provider, model, endpoint, or the
disposable confirmation is absent. The live command repeats the synthetic probe and warm-up
before creating its disposable scenario database. Connection, response/read, and warm-up use
separate defaults of 5, 120, and 300 seconds. The corresponding CLI flags are
`--connect-timeout-seconds`, `--model-timeout-seconds`, and `--warmup-timeout-seconds`; none is an
unbounded retry and provider transport failures are not retried.

Before opening the prompt, live mode runs mandatory disposable scenarios: a model-required stable
fact, bounded category reconfirmation, current-location creation and retraction, ambiguous-goal
review persistence, invalid-schema safety, and synthetic sensitive/prohibited leak checks. If a
mandatory scenario fails, interactive continuation is disabled and the process exits nonzero.
After a successful battery, enter more messages or `:quit`. At exit it prints:

```text
deterministic_applied_count=<count>
live_model_call_count=<count>
live_model_transport_success_count=<count>
live_model_valid_schema_count=<count>
live_model_invalid_schema_count=<count>
live_model_transport_failure_count=<count>
live_model_applied_count=<count>
live_model_review_count=<count>
live_model_no_action_count=<count>
phase4_live_model_validation=PASS
```

The live-only counters select results whose diagnostic provider matches the configured live
provider. Deterministic applications and the expected invalid-schema fixture are excluded.
`PASS` requires at least one transport success, one strictly valid schema response, and one
grounded model-assisted application or review, plus all deterministic and leakage invariants. A
provider that safely rejects every response as
`invalid_model_schema` is fail-closed but not functioning; it prints
`phase4_live_model_validation=FAIL` and exits nonzero.

The Ollama request contains the configured model, a bounded extraction-only system instruction,
the bounded `ModelExtractionInput` as one user message, `stream=false`, and temperature zero.
Schema mode puts the strict Pydantic JSON schema object in `format`; JSON fallback sends
`format="json"` and then applies the identical strict client schema. `think=false`, deterministic
seed, prediction limit, and `keep_alive` are included only when their individual probes succeed.
The system instruction still forbids reasoning transcripts when `think` is unavailable.
The recognized success envelope is a JSON object with `message.role=assistant` and a string
`message.content`; content is then parsed and schema-validated. Raw content and reasoning are
never printed. One exact `json` code fence is supported; prose around JSON, arbitrary brace
searching, partial streams, unknown envelopes, and malformed/truncated content are rejected.
Ollama `{ "error": ... }` envelopes map to bounded codes including
`ollama_unsupported_format_schema`, `ollama_unknown_field_think`,
`ollama_model_not_found`, `ollama_invalid_model_name`, `ollama_invalid_options`,
`ollama_request_too_large`, `ollama_invalid_request`, and `ollama_server_error`. Durable
diagnostics retain only the code, status, envelope shape, size, and hash. Manual probe output may
show at most 240 allowlisted characters of provider text, and suppresses text containing the
request, sensitive markers, or secret markers.

## Copy-paste prompt battery

Start with a reset profile. Submit each paragraph as a separate user input.

```text
I want to create long-form cinematic YouTube videos.

I no longer want to make long-form cinematic YouTube videos. I want to create short Instagram reels clearly.

What I said is a goal, not a preference.

For video-editing advice, give me quick 15-minute drills.

Always answer me concisely.

I am drinking coffee right now and I have a headache because I slept late.

My friend prefers Rust.

Maybe I will learn Rust someday.

I prefer project-based learning, not only video courses.

I no longer live in Pune.

I live in Pune.

I currently live in Delhi.

My current city is Mumbai.

I am visiting Pune.

I am in Pune right now.

Now I want to learn Japanese.
```

Expected results:

1. A durable `goal` candidate in `video_creation` becomes one active canonical goal.
2. The old clause and new clause are separately grounded. Deterministic owner-bound resolution
   issues one replace command. The old goal becomes `superseded`; only
   `create short Instagram reels clearly` remains active in
   `goal:video_creation:current_primary_goal`. `clearly` stays value text.
3. The proposal is typed as a goal rather than a preference. If the prior value is already a
   goal, the result is a reconfirmation/no duplicate; a genuine grounded category correction is
   routed through the explicit correction contract. A reconfirmation has an empty
   `suppressed_memory_ids` array.
4. A domain-scoped preference is created under `video_creation`, with the
   practice/advice-format dimension. It is not global response style.
5. A separate global response-style preference is created because `Always` supplies genuine
   global scope.
6. No durable candidate or canonical record is created; the pre-parser marks a temporary state.
7. No user preference is created because the subject is a third party.
8. No current durable goal is created because the statement is hypothetical.
9. A learning-format preference may be created. `not only` is not parsed as a retraction, so no
   removal of video courses occurs.
10. If a matching active Pune location exists, it is archived through the approved pure
    retraction path. Otherwise the target remains unresolved/reviewable. A canonical value such
    as `not Pune` must never be created.
11. The three unambiguous residence forms create a grounded `identity` value in
    `identity:global:current_location`; Pune, Delhi, and Mumbai are examples, not hardcoded values.
12. Visiting and current-moment location forms do not create permanent residence.
13. With an occupied exclusive Japanese-learning target that cannot be linked reliably, create
    a `needs_review` candidate and leave the existing active record unchanged. With no conflict,
    it may be an independent goal. The model may not invent a predecessor ID. Candidate-target
    and unresolved-conflict fields may identify the review context, but suppression fields must
    remain empty.

After each accepted mutation, inspect that the candidate is `applied` and linked to an operation;
after an ambiguous correction, inspect that the candidate is `needs_review` and no canonical
write occurred. Assistant/system/tool spans, bad offsets, or invented values must appear only as
rejections. Nothing in this procedure demonstrates that Neo later recalled or used a memory.
