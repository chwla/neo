# Neo memory v2 Phase 5 recall design

Status: implemented for disabled, owner-cohort-gated validation; pending independent manual
validation. This document does not authorize rollout.

## Authoritative path

When canonical query recall is enabled for an authenticated owner, all Phase 5 consumers use:

```text
memory_records_v2
→ SQL owner + active + unexpired eligibility
→ deterministic, scoped lexical, or broad selection
→ sensitivity and current-turn suppression
→ bounded RecallResult
→ escaped untrusted user-context serialization
→ usage update for final serialized canonical IDs only
```

Legacy typed tables, legacy generic memory search/FTS, semantic embeddings, vector indexes,
Qdrant archives, and process-default research sessions are not fallback truth on this path.
Legacy behavior remains unchanged while the new flags are disabled.

## Query and eligibility contract

`MemoryQueryContext` requires an authenticated owner UUID, database identity, profile, gates,
request/session identifiers, current time, allowed types/domains, record and character budgets,
mode, lexical state, and optional Phase 4 `CurrentTurnOverride`. The owner-bound repository still
validates the database's single owner binding during construction.

Every normal-serving SQL statement includes:

```sql
memory_records_v2.owner_id = :authenticated_owner
AND memory_records_v2.status = 'active'
AND (
  memory_records_v2.expires_at IS NULL
  OR memory_records_v2.expires_at > :current_time
)
```

Exact canonical ID, exact type/domain/slot, trusted-slot, lexical input, broad input, active
source-ID trace, and usage operations all preserve the owner predicate. Historical canonical
states and review candidates are not queried as normal-serving truth.

The established schema represents logical deletion as `forgotten`; permanent erasure removes
the canonical row while retaining only non-serving audit/tombstone material. `needs_review` and
`rejected` are candidate states in `memory_candidates_v2`, never serving states in
`memory_records_v2`. The Phase 5 lifecycle fixture verifies active recall alongside superseded,
archived, forgotten, expired, needs-review, and rejected shapes and proves the serving statement
references only `memory_records_v2`.

## Recall modes and scoring

The shared recall service exposes deterministic, scoped lexical, and broad modes. Deterministic
lookups do not require lexical infrastructure. Scoped mode tokenizes Unicode-normalized,
case-folded words and computes normalized BM25-style overlap. Zero lexical overlap is never made
relevant by recency, importance, confidence, usage, or pinning. Allowed domains are enforced in
SQL and remain a strong gate.

Scoring policy `neo.memory.lexical.bm25.v1` uses bounded weights:

| Component | Maximum contribution |
| --- | ---: |
| Domain fit | 0.25 |
| Normalized lexical score | 0.45 |
| Importance | 0.08 |
| Confidence | 0.07 |
| Confirmation freshness | 0.05 |
| Record recency | 0.04 |
| Log-capped usage | 0.03 |
| Pin | 0.03 |

The scoped threshold defaults to `0.18`. Normal prompt selection defaults to five records and
2,400 serialized characters. Ordering is deterministic by total score, confirmation/update
time, then canonical ID. Selection deduplicates canonical IDs and semantic slots and applies a
broad type-diversity policy. Pin is only a bounded rank boost.

If lexical recall is unavailable, scoped recall returns no memory. Deterministic lookup and
bounded broad canonical ranking continue without a vector or recent-table dump.

## Sensitivity and current-turn precedence

Sensitive records are filtered before text access during ordinary and broad recall. An explicit,
deterministic authorized lookup can return only the redacted display marker
`[sensitive memory]`; Phase 5 does not decrypt sensitive plaintext. Prohibited records cannot be
persisted and therefore cannot be recalled.

Phase 4 explicit `suppressed_memory_ids` and `suppressed_slot_keys` are removed before
serialization and usage accounting. Candidate targets, unresolved conflicts, or reconfirmation
alone do not suppress. Positive current assertions remain in the current user turn and are not
duplicated as durable memory context.

## Prompt and usage semantics

Serializer `neo.memory.prompt.v1` emits one separate `user` message whose header name is
`neo_untrusted_memory_context`. XML-like text and attributes are HTML-escaped, so closing tags or
role-like markup remain inert data. Only canonical ID, type, domain, and approved display text
are included. The stable system policy is independent of recalled values.

Usage is updated only after successful final serialization, for exactly the serialized IDs.
Fetched, filtered, suppressed, diversity-dropped, over-budget, expired, inactive, or sensitive
candidates receive no usage. The update changes `usage_count` and `last_used_at`, not canonical
revision. A savepoint prevents partial usage updates; usage failure is reported with a bounded
code and does not break foreground prompt construction.

## Consumers and isolation

- Retrieval returns one canonical `RecallResult` and no legacy/archive personal collections.
- Direct answers use the same recall service and expose returned canonical IDs.
- Context carries `RecallResult` instead of assembling parallel v2 truth.
- Sync and stream both call `NeoChatService.build_messages`, which invokes one shared canonical
  orchestration method.
- API chat construction derives owner/profile/database binding from the authenticated session
  and the already profile-bound SQL session; no request-body owner is accepted.
- Research requires injected authenticated owner/profile/database wiring and uses the same
  untrusted wrapper; missing wiring fails memory closed while research may continue.
- Current ownerless Qdrant archives always return an empty personal-memory context.
- Incognito and memory-disabled gates occur before repository query, serialization, or usage.

## Flags and phase boundary

Canonical query, lexical recall, secure prompt, direct-answer reads, and research recall all
default to `False`. Legacy read compatibility defaults to `True`. Subfeatures cannot be enabled
without canonical queries, schema availability, and an owner allowlist. Research recall also
requires secure prompt serialization.

No embeddings, vector calls, derived-index workers, migration, canary rollout, cutover, or other
Phase 6 infrastructure were added. Independent manual validation is required before Phase 5 can
be declared complete or checkpointed.
