# Neo memory v2: Phase 0 decisions

## Status and scope

Phase 0 freezes the first contract, taxonomy, and policy vocabulary before any schema or
runtime work. The authoritative identifiers are:

| Contract | Version |
|---|---|
| Command/result contract | `neo.memory.contract.v1` |
| Taxonomy | `neo.memory.taxonomy.v1` |
| Product policy | `neo.memory.policy.v1` |

The files under `app/services/memory_v2/` are Pydantic/dataclass/enum contracts only. They
import no SQLAlchemy objects and are not connected to chat, APIs, repositories, extraction,
retrieval, indexes, or background jobs.

## Command envelope

Every command contains:

- `contract_version`, `policy_version`, and `taxonomy_version`;
- a required canonical lowercase UUID `owner_id` (finalized in Phase 1);
- a required, nonblank owner-scoped `idempotency_key`;
- typed `actor` and `source` objects;
- an operation discriminator;
- operation-specific payload;
- optional `dry_run`.

Known-record mutations use `TargetRevision(memory_id, expected_revision)`. Create has no
expected revision because no canonical record exists. Replace can carry zero or more known
target revisions plus typed target evidence; a grounded same-slot replacement may resolve its
target transactionally in Phase 2. All contracts forbid unknown fields.

### Actor kinds

| Code | Meaning |
|---|---|
| `user` | Authenticated user action |
| `system` | Neo-owned deterministic action |
| `agent` | Authorized agent tool acting through the same contract |
| `migration` | Future migration runner |
| `maintenance` | Future deterministic maintenance command |

UI versus HTTP is provenance, not a different actor authority.

### Source kinds

`chat_message`, `direct_command`, `manual_ui`, `http_api`, `agent_tool`,
`automatic_extraction`, `review`, `import`, `migration`, and `maintenance`.

Sources may identify a session, conversation, message, source object, observation time, and
typed assertion/retraction spans. Negative predecessor language belongs in a retraction span or
target hint, never in the canonical replacement value.

## Stable operations

| Operation | Typed payload | Phase 0 semantic contract |
|---|---|---|
| `create` | validated positive candidate | Create an independent fact; Phase 2 will reconfirm duplicates and reject occupied exclusive slots that require replace. |
| `update` | target revision and nonempty compatible patch | Refine a known record; cannot change identity through the patch. |
| `replace` | validated replacement candidate, authority, optional target revisions | Atomically replace deterministic conflicts; candidate intent must be `replace` and target evidence is normally required. |
| `supersede` | one or more predecessor revisions and successor ID | Internal first-class lifecycle primitive; supports many predecessors. |
| `merge` | at least two source revisions and validated merged candidate | Typed compatible merge only; arbitrary text concatenation is outside the contract. |
| `archive` | target revision | Reversible removal from current recall with no successor. |
| `forget` | target revision | Remove recallable content/indexes and retain a 30-day owner-scoped keyed-fingerprint tombstone. |
| `erase_permanently` | target revision | Remove canonical content, provenance, tombstone, eligible audit content, and derived data within retention capabilities. |
| `restore` | target revision, restore mode, optional replacement candidate | `archived_only` restores only a free archived slot; `as_replacement` requires a new validated candidate. |

There is deliberately no ambiguous `delete` operation.

## Candidate contracts

`CandidateProposal` can represent proposed or rejected evidence. It includes first-class:

- subject, memory type, domain, slot, and cardinality;
- positive typed JSON `canonical_value` and positive `display_text`;
- sensitivity, confidence, importance, schema/policy/taxonomy versions;
- assertion or replacement intent;
- explicit-user-request flag;
- target IDs, old-value phrases, predecessor domain/slot, and explicit identity-change flags;
- assertion and retraction evidence spans.

`ValidatedCandidateProposal` is the only candidate accepted by mutation commands. It rejects
`prohibited` sensitivity, and rejects `sensitive` candidates without explicit user request.
The policy classifier is still authoritative: it reclassifies the actual text so a caller cannot
label an API key `normal` and bypass the rule.

Candidate lifecycle codes are `proposed`, `validated`, `needs_review`, `applied`, `rejected`,
and `expired`. Durable lifecycle codes are `active`, `superseded`, `archived`, and `forgotten`.
Permanent erasure has no durable lifecycle row afterward.

## Stable outcomes

| Outcome | Meaning |
|---|---|
| `created` | New active record committed |
| `reconfirmed` | Existing canonical value received new supporting evidence |
| `refined` | Compatible update committed |
| `replaced` | Replacement committed and conflicts superseded |
| `superseded` | Explicit supersede primitive committed |
| `merged` | Compatible merge committed |
| `archived` | Record archived |
| `forgotten` | Recallable content removed under 30-day tombstone policy |
| `erased_permanently` | Erasure completed within retention capabilities |
| `restored` | Safe restore committed |
| `needs_review` | Proposal retained for review; prior active record remains active |
| `rejected` | Policy or deterministic admission rejected the operation |
| `disabled` | Memory was disabled before any memory work occurred |
| `failed` | Technical command failure with a stable error code |

`needs_review`, `rejected`, and `disabled` require a rejection code. `failed` requires an error
code. Successful outcomes cannot carry either kind of code.

## Stable rejection codes

| Code | Use |
|---|---|
| `ambiguous_conflict` | Incompatible occupied-slot assertion lacks deterministic replacement authority |
| `conflict_requires_replace` | Create/update attempted where explicit replace semantics are required |
| `incognito_disabled` | Incognito blocked the entire memory operation |
| `memory_disabled` | Personal memory is disabled |
| `prohibited_sensitive_content` | Secret or prohibited content cannot be persisted |
| `sensitive_requires_explicit_request` | Sensitive fact was proposed by automatic extraction |
| `resurrection_blocked` | Automatic recreation matched a live forget tombstone |
| `replacement_target_not_found` | Deterministic replacement target could not be resolved |
| `positive_value_required` | Candidate has no clean positive canonical value |
| `ungrounded_candidate` | Candidate is not grounded in authorized user evidence |
| `too_many_candidates` | Extraction/batch policy limit exceeded |
| `invalid_restore` | Requested restore mode violates lifecycle rules |

## Stable error codes

| Code | Use |
|---|---|
| `invalid_command` | Contract-valid envelope contains an unsupported semantic combination |
| `owner_required` | Authenticated owner is missing |
| `owner_mismatch` | Command owner and execution owner disagree |
| `cross_owner_reference` | A referenced object belongs to another owner |
| `not_found` | Authorized target does not exist |
| `expected_revision_required` | Known-record mutation omitted concurrency control |
| `revision_conflict` | Expected and current revisions differ |
| `idempotency_conflict` | Same owner/key was reused with a different request |
| `unsupported_contract_version` | Command/result contract version is unsupported |
| `unsupported_policy_version` | Policy version is unsupported |
| `unsupported_taxonomy_version` | Taxonomy version is unsupported |
| `internal_error` | Non-policy implementation failure |

## Exact initial ontology

No other known domains exist in v1.

| Domain | Deterministic aliases in v1 |
|---|---|
| `global` | `global`, `across all topics`, `for every topic` |
| `communication` | `communication`, `public speaking`, `presentation skills`, `writing` |
| `software_development` | `software development`, `programming`, `coding`, `code review` |
| `learning` | `learning`, `studying`, `study skills` |
| `career` | `career`, `job search`, `job interview`, `professional development` |
| `finance` | `finance`, `financial`, `budgeting`, `investing` |
| `health_fitness` | `health and fitness`, `health fitness`, `fitness`, `exercise`, `workout`, `nutrition` |
| `travel` | `travel`, `trip planning`, `vacation planning` |
| `video_creation` | `video creation`, `video editing`, `edit videos`, `youtube creation`, `youtube videos`, `cinematic youtube`, `instagram reels`, `short form video`, `short form videos`, `reels creation` |
| `gaming` | `gaming`, `video games`, `computer games` |

Aliases are normalized for case, punctuation, and hyphens and matched longest-first. Video
editing, YouTube creation, Instagram reels, and short-form video therefore share
`video_creation`.

Unknown domains use `topic.<grounded_normalized_phrase>`, for example `topic.astronomy`.
The caller must supply a phrase that occurs in the source. There is no last-token fallback.
Value-only terms such as `clearly`, `quickly`, `confidently`, and `concisely` cannot form a
single-token unknown domain.

Memory types are `identity`, `preference`, `goal`, `project`, `education`, `employment`,
`activity`, `event`, and `knowledge`.

## Slot and cardinality rules

| Type/role | Slot form | Cardinality |
|---|---|---|
| Identity key | `identity:global:<identity_key>` | Exclusive |
| Preference dimension | `preference:<domain>:<dimension>` | Exclusive |
| Global response style | `preference:global:<dimension>` | Exclusive |
| Independent goal | `goal:<domain>:independent:<entity_uuid>` | Additive |
| Goal role `primary_output` | `goal:<domain>:primary_output` | Exclusive |
| Goal role `current_primary_goal` | `goal:<domain>:current_primary_goal` | Exclusive |
| Current education/employment status | `<type>:<domain>:current_status` | Exclusive |
| Project, historical education/employment, activity, event, independent knowledge | `<type>:<domain>:item:<entity_uuid>` | Additive |

Opaque UUID entity components prevent additive slots from being formed from remembered values.
Unsupported goal roles fail rather than silently becoming exclusive. A correction with a
reliably identified predecessor inherits its domain, slot, and cardinality. An explicit domain
change rewrites only the domain component while preserving the predecessor role; an explicit
slot change must supply a complete matching slot.

## Approved policy constants

### Sensitivity

- `normal`: automatic or explicit persistence is allowed after other validation.
- `sensitive`: automatic persistence is rejected; explicit user request is required. It is
  recallable only when directly relevant, never merely because recall is broad or the record is
  pinned.
- `prohibited`: never durable or recallable.

Initial prohibited detection covers supplied passwords, OTP/verification codes, API keys,
access/authentication/client secrets, private-key blocks, token-shaped `sk-...` values, and full
Luhn-valid payment-card numbers. Initial sensitive detection covers clearly personal health
disclosures, government identifiers, financial-account identifiers, and exact private street
addresses. This deterministic list can expand only with a policy-version change and tests.

### Deletion and resurrection

- `forget`: remove recallable content and all derived data; retain keyed owner-scoped
  fingerprint for exactly 30 days; block automatic recreation; allow explicit reconfirmation.
- `erase_permanently`: retain no fingerprint tombstone and request deletion of canonical,
  provenance, eligible audit, and derived content according to storage/backup capabilities.
- Superseded content is authorized-history-only until forgotten or permanently erased.

### Conflict

Explicit corrections, linked retraction/replacement statements, and strongly grounded same-slot
assertions apply automatically only when the target relationship is deterministic. Otherwise
the outcome is `needs_review`, and the existing record remains active. Timestamp, confidence,
vector similarity, and newest-wins are not replacement authority. Current user text overrides
contradictory stored context for the current response.

### Pin, usage, and limits

- Pin is ranking boost only, capped at `0.05` in the initial score contract. It bypasses nothing
  and never guarantees inclusion.
- Usage events are diagnostic only; `USAGE_AFFECTS_RANKING = false`.
- Normal recall: at most 5 records and 2,400 serialized characters.
- Automatic extraction: at most 4 candidates per turn.
- Explicit command/import batch: at most 50 candidates, with per-item command outcomes.

### Timing and modes

- Explicit memory commands: before response.
- Deterministic high-confidence corrections: before response plus current-turn contradiction
  overlay.
- General automatic LLM extraction: after the turn.
- Guest: isolated ephemeral profile store.
- Incognito: no retrieval, direct answer, extraction, candidate, mutation, usage, index, or
  background memory work. An explicit command returns `disabled/incognito_disabled` with no
  affected records.

## Discrepancies from the six earlier documents

1. Earlier specifications used `delete`/`deleted`. The approved contract replaces them with
   `forget`/`forgotten` and `erase_permanently`; permanent erasure leaves no lifecycle row.
2. Sensitivity was previously a policy concern but not a required first-class candidate/record
   field. It is now mandatory and cannot be hidden in metadata.
3. Earlier prose sometimes described current goals as usually exclusive. Approved v1 goals are
   additive by default; only `primary_output`, `current_primary_goal`, or inherited exclusive
   predecessor slots are exclusive.
4. Earlier recall scoring included usage. Usage is now diagnostic only for the initial release.
5. Extraction timing was previously unresolved. Foreground explicit/correction processing,
   post-turn general LLM extraction, and the transient current-turn overlay are now fixed.
6. Guest, incognito, pinning, deletion retention, and the known domain set are no longer open
   product questions.
7. `employment` is now an explicit memory type because the approved cardinality policy names
   employment history and current employment status independently.

These approved Phase 0 decisions supersede the conflicting earlier wording.

## Remaining non-blocking decisions

These do not change the Phase 0 vocabulary or prevent Phase 1 schema work:

- Exact typed `canonical_value` schemas and preference-dimension vocabulary per memory type are
  Phase 2 normalizer work; Phase 1 must retain `value_schema_version`.
- Phase 1 finalized owner identity as a canonical lowercase UUID. The command contract now
  validates and normalizes UUID owner IDs; the profile registry persists the stable mapping.
- The keyed fingerprint algorithm, secret rotation, and backup enforcement mechanism must be
  selected with Phase 1/operational design while preserving the 30-day contract.
- “Eligible audit content” and permanent-erasure behavior in immutable backups must be mapped to
  actual retention capabilities before production enablement.
- At-rest protection for explicitly saved sensitive values must be selected before production
  enablement; sensitivity remains a required schema field regardless of encryption mechanism.
- Strongly grounded same-slot matching thresholds are deterministic Phase 2 planner rules, not
  vector/confidence thresholds.
- Alias additions, translated aliases, and new known domains require a taxonomy-version change;
  they are not silently learned from values.
- The 2,400-character recall budget may later become a model-token budget under a new policy
  version; Phase 0 uses characters so the contract is provider-independent.

## Phase 1 readiness

Phase 1 can begin safely as schema-only work. The command, result, lifecycle, sensitivity,
owner, idempotency, revision, taxonomy, slot/cardinality, and deletion requirements needed by
the schema are frozen and tested. Phase 1 must not enable runtime reads/writes or defer the keyed
tombstone and sensitivity fields. Production enablement remains gated on the non-blocking
operational choices above and all later phase acceptance criteria.
