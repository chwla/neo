# Neo memory: current-state audit

## Scope and evidence

This audit covers Neo's personal conversational memory, its typed projections, lifecycle and indexing services, chat integration, HTTP entry points, profile isolation, and the two other subsystems that also use the word “memory.” It distinguishes current behavior from the redesign proposed in `neo-memory-redesign-spec.md`.

The repository already contained uncommitted memory work when this audit began (`app/services/chat.py`, `direct_answer.py`, `extraction.py`, `retrieval.py`, `review.py`, the new `memory_scope.py`, and `tests/`). Those files were inspected as the current implementation and were not changed. The current suite is green (`.venv/bin/python -m pytest -q`: 51 passed), but the critical unstructured correction from the brief still produces two active goals. Green regression tests therefore do not establish a sound architecture.

## Current architecture

### Canonical-looking record and typed projections

`app/models/memory.py:22-71` defines `Memory`, the record used by generic recall and lifecycle code. It has an integer ID, text, type, confidence and importance, source fields, a fingerprint, `canonical_slot`, expiration, lifecycle strings, active flags, one predecessor/successor link, timestamps, provenance rows, project links, and an embedding row.

At the same time, facts are independently stored in category tables:

| Category | Current table/model | Independent state |
|---|---|---|
| Identity/profile | `app/models/profile.py:8-21`, `ProfileFact` | `key`, `value`, `is_active` |
| Preference | `app/models/preference.py:8-25`, `Preference` | category/value/slot/fingerprint/active |
| Goal | `app/models/goal.py:11-32`, `Goal` | description/status/fingerprint |
| Project | `app/models/project.py:10-38`, `Project` | name/description/status |
| Education | `app/models/education.py:10-26`, `Education` | institution/degree/field/active |
| Activity | `app/models/activity.py:10-27`, `Activity` | category/name/time/active |
| Event | `app/models/event.py:10-36`, `Event` | title/time/location; no common lifecycle |

These are not read-only projections. They are mutable stores used directly by APIs and direct answers. Consequently Neo has no single source of truth: a fact can exist only in `memories`, only in a typed table, or in both with different text, status, fingerprint, and index state.

Supporting records are:

- `MemoryCandidate` (`app/models/memory_candidate.py:20-52`): pending/accepted/rejected/merged proposal. Structured attributes are embedded in a free-form JSON `reasoning` string.
- `MemorySource` (`app/models/memory_source.py:8-34`): message/conversation provenance and detachment state.
- `MemoryEmbedding` (`app/models/memory_embedding.py:12-31`): JSON vector, provider/model/hash/status in the same SQLite database.
- `MemoryLifecycleAudit` (`app/models/memory_lifecycle_audit.py:9-26`): append-only-looking lifecycle events.
- Enums in `app/models/enums.py:25-53`: candidate and type enums. Durable lifecycle values are instead raw strings in `app/services/lifecycle.py:10-13`.

### Persistence and tenancy

`app/db/session.py:10-18` uses SQLite with WAL, foreign keys, and a busy timeout. `initialize_database` (`app/db/session.py:58-70`) combines `Base.metadata.create_all` with hand-written additive `ALTER TABLE` helpers rather than a versioned migration framework. `ensure_memory_metadata_columns` (`:144-171`) does not add every field now present on `Memory`, while parts of the embedding/status backfill are conditional on first creating the embedding table (`:226-293`). An existing database can therefore skip later repairs.

Neo isolates profiles primarily by physical database. `ProfileAccountService.database_url_for` and `profile_database_context` (`app/services/profile_accounts.py:83-99`) choose a database through context variables. `ProfileDatabaseMiddleware.dispatch` (`app/main.py:86-95`) applies that context for a profile session. There is no `owner_id` on personal memory rows and no record-level owner predicate. A request with no profile session falls through to the process-default database, and background work or service use outside the profile context can do the same. Guest mode creates a temporary profile database on disk (`app/services/profile_accounts.py:196-204`); it is not a turn-level “no retrieval and no persistence” mode.

## Mutation-path inventory

There is no common mutation command or transaction boundary. The following paths can change personal memory.

| Entry point | Call path | Behavior and divergence |
|---|---|---|
| Ordinary chat, sync | `app/api/routes/memory.py:888-932` → `NeoChatService.send_message` (`app/services/chat.py:200-594`) → `persist_user_memory` (`:1426-1518`) | Runs extraction before recall/answer on every ordinary prompt, accepts candidates, repairs legacy identity sources, and commits internally. |
| Ordinary chat, stream | route `:983-1030` → `NeoChatService.stream_message` (`chat.py:632-1424`) | Duplicates much of the sync orchestration and its mutation ordering. |
| Optional post-turn extraction | `NeoChatService.extract_after_turn` (`chat.py:2657-2665`) | A second automatic path, deterministic only and without equivalent source context. |
| Conversation ingest | `POST /conversation`, `memory.py:785-805` | Deterministic extraction/persistence plus separate conversation archival. |
| Direct extraction API | `POST /extract-memory`, `memory.py:808-818` | Bypasses chat orchestration and model-assisted admission behavior. |
| Candidate review | `POST /memory/review`, `memory.py:831-841` → `MemoryReviewService.review` (`app/services/review.py:56-137`) | Category-specific acceptance and merge logic; creates or mutates typed rows and `Memory`. |
| Manual generic create | `POST /memories`, `memory.py:1385-1401` → `MemoryStore.create_manual_memory` (`app/repositories/memory_store.py:868-904`) | Exact manual fingerprint only; bypasses candidate validation, category projection, conflict matching, and replacement semantics. |
| Generic patch | `PATCH /memories/{id}`, `memory.py:1403-1420` → `MemoryStore.update_memory` (`memory_store.py:839-866`) | Mutates canonical-looking text without maintaining typed projections or reliably recomputing identity/index state. |
| Generic delete | `DELETE /memories/{id}`, `memory.py:1422-1429` → `MemoryStore.delete_memory` (`memory_store.py:1012-1020`) | Lifecycle delete of `Memory`; typed representation may diverge. |
| Explicit natural-language removal | `NeoChatService._handle_memory_action` (`chat.py:1593-1658`) → `delete_memories_matching_explicit_removal` (`memory_store.py:1022-1071`) | Token-overlap matching can remove a memory when any target token overlaps, broader than the function contract implies. |
| Typed profile/preference/goal/project/event CRUD | routes `memory.py:1236-1377`, `:1567-1627` → corresponding `MemoryStore.update_*`/`delete_*` (`memory_store.py:660-837`) | Directly mutates typed rows, then attempts text-based changes in `Memory`; semantics differ per type. |
| Typed education/activity CRUD | routes `memory.py:1152-1234` → `MemoryStore.update_education`/`update_activity` (`memory_store.py:906-1010`) | Has more explicit index synchronization than most typed paths, another behavioral variant. |
| Project creation | `POST /projects`, `memory.py:1271-1280` → `MemoryStore.create_project` (`memory_store.py:294-300`) | Creates typed project state without passing the candidate/review conflict pipeline. |
| Archive/supersede/restore APIs | routes `memory.py:1431-1483` → `MemoryLifecycleService` via store wrappers | Operate on `Memory` lifecycle independently of typed record semantics. |
| Message edit/rerun | routes `memory.py:1033-1134` → source detachment and chat re-extraction | `MemoryStore.detach_memory_sources_for_message` (`memory_store.py:485-538`) may archive final-source records; replacement then creates new candidates. |
| Chat deletion | `DELETE /chats/{id}`, `memory.py:1136-1145` → source detachment/deletion | Last-source facts can be tombstoned even if semantically reconfirmed elsewhere but not linked correctly. |
| Aging | `POST /memory/lifecycle/age`, `memory.py:1497-1519` → `MemoryStore.age_memories` (`memory_store.py:1220-1231`) → `MemoryLifecycleService.age` (`app/services/lifecycle.py:199-271`) | Archives eligible records according to importance/type/age. |
| Maintenance/compression | `POST /memory/lifecycle/maintenance`, `memory.py:1521-1545` → `MemoryLifecycleMaintenance.run` (`app/services/lifecycle_maintenance.py:116-197`) | Archives duplicates selected by exact/identity heuristics; audit repair is another direct writer. |
| Reflection | `POST /reflection/run`, `memory.py:844-852` → `ReflectionService.run` (`app/services/reflection.py:26-67`) | Reads typed goals/projects plus generic memories, synthesizes a “current focus” sentence, then persists and accepts it as a generic memory candidate. Stale/contradictory inputs can therefore become a new durable summary. |
| Import/migration-like repair | startup schema helpers and `NeoChatService._repair_invalid_identity_sources` (`chat.py:1536-1565`) | Schema and data repair happen opportunistically, including during a user prompt, rather than through a migration ledger. |

`MemoryStore.add` (`memory_store.py:99-113`) also has implicit side effects: adding a `Memory` writes FTS, calls the embedding provider, and adds a “created” audit. Callers that appear to perform one SQL insert therefore invoke derived work inside the canonical transaction.

### Candidate validation, duplication, and conflict paths

`MemoryExtractionService` (`app/services/extraction.py:77-2899`) has three overlapping decision layers:

1. A large deterministic regex extractor (`extract`, `:164-226`, and category parsers through `:2128`).
2. Model extraction, admission review, retries, and local fallback (`extract_with_llm`, `:505-647`).
3. Model/local merge and auto-accept rules (`_finalize_model_merge`, `:649-672`; `_should_auto_accept`, `:1012-1054`).

Candidate deduplication inside a turn is exact normalized text or a category-specific semantic key (`:2363-2412`). Domain, scope, and slot creation are spread across `extraction.py`, `app/services/memory_scope.py`, `MemoryReviewService`, `MemoryConflictService` (`app/services/conflicts.py`), and the module-level `tombstone_identity` helper (`lifecycle.py:317-376`). These algorithms do not share one identity contract.

`MemoryReviewService._accept` (`review.py:170-640`) is effectively several mutation implementations:

- identity deactivates matching profile keys;
- preferences have replacement, refinement, domain, and slot heuristics;
- goals use token-overlap refinement plus a separate explicit-replacement path;
- projects mostly use exact matching;
- activities replace categories;
- events use fingerprints;
- generic knowledge only has special conflict handling for current hardware.

`MemoryReviewService._merge` (`review.py:139-168`) concatenates candidate text to an existing active memory with a newline. It does not define semantic merge validity and does not update every associated typed projection.

`MemoryConflictService._conflicts` (`app/services/conflicts.py:55-62`) treats non-preference/non-identity records as conflicts only when normalized text is equal. Thus the component named for conflict detection cannot identify the central old-goal/new-goal conflict.

## Retrieval-path inventory

| Consumer | Current source(s) | Important behavior |
|---|---|---|
| Generic context API | `POST /retrieve-context`, `memory.py:821-828` → `RetrievalService.retrieve` (`app/services/retrieval.py:51-132`) | Returns profile, preferences, goals, projects, events, generic memories, and archive results as separate lists. The same fact may be returned twice. |
| Generic memory search | `MemoryStore.search_memories` (`memory_store.py:1233-1373`) | Uses hybrid FTS/semantic search when available, active SQL filters, and hard-coded query/slot expansions. |
| Conversation archive search | `RetrievalService.retrieve` (`retrieval.py:76-83`, `:177-182`) → `QdrantArchiveService.search` (`app/services/archives.py:64-85`) when explicitly configured | Searches whole archived conversation/document/note payloads. Qdrant payloads have no owner field or canonical join. The default chat path disables archives, but enabling this service would create a cross-profile retrieval risk. |
| Direct memory answers | `NeoChatService._direct_reply` (`chat.py:1715-1718`) → `DirectMemoryAnswerService.answer` (`app/services/direct_answer.py:14-88`) | Routes many regex-recognized questions to typed-table queries; other summaries read `Memory`. Two users asking equivalent questions can exercise different sources of truth. |
| Prompt context | `NeoChatService.build_context` and `build_messages` (`chat.py:118-198`) | Compacts typed and generic lists in `_compact_context` (`:2206-2225`) and interpolates them into a system message labeled as memory context. It is not separated as untrusted data. |
| Background research planning | `retrieve_scoped_memory` (`app/services/research/memory_scope.py:35-61`) → `_build_scoped_context` (`:64-88`) → `run_research_job` (`app/services/research/jobs.py:150-179`) | Opens process-global `SessionLocal` rather than receiving an authenticated owner/profile context, reads typed goals/projects/profile and generic hardware, and appends text into planner user content (`app/services/research/planner.py:86-87`). A background job can therefore read the default/wrong profile database and bypass normal recall/security rules. |
| Country and web helpers | `chat.py:2162-2204` | Read profile/context again for routing and query enrichment. |
| Sidebar/UI lists | `/goals`, `/education`, `/activities`, `/projects`, `/events`, `/profile`, `/preferences`, `/memories` in `memory.py:1147-1627` | Some list typed projections; generic list reads `Memory`; visible state can disagree. |
| Lifecycle/history | `/memories/{id}/lifecycle`, `memory.py:1485-1495` | Returns audit for a `Memory` ID only, not the full typed-record or multi-predecessor history. |

The semantic path (`MemoryStore._search_memories_semantic`, `memory_store.py:1348-1373`) joins embedding rows back to `Memory`, which prevents an embedding row from being returned alone. That is useful, but it does not solve stale content hashes, missing indexes, owner isolation, or inconsistent canonical state. FTS is lazily created and backfilled only when its row count is zero (`_ensure_memory_fts`, `:1375-1395`), so a partially missing or stale table is not reconciled automatically.

`POST /conversation` also writes the entire supplied conversation to Qdrant through `QdrantArchiveService.archive_text` (`app/api/routes/memory.py:785-805`) with metadata containing only `source="conversation"`. The archive point has no owner, profile, or canonical lifecycle. This archive is not enabled in ordinary chat recall today, but it must not be treated as a safe personal-memory source unless it gains strict owner metadata, authorization, deletion, and canonical-join semantics.

Recall usage accounting is inconsistent. `RetrievalService.retrieve` changes `last_accessed_at`, but ordinary chat later rolls back its read session while several direct-answer paths commit. A read therefore has path-dependent write semantics.

## Lifecycle behavior

`MemoryLifecycleService` (`app/services/lifecycle.py`) defines active, archived, deleted, and superseded strings. Its methods remove FTS rows and mark embeddings stale:

- `supersede` (`:35-58`) deactivates the old row and links it to one replacement. `new.supersedes_id` can retain only one predecessor even when a correction replaces several facts.
- `archive` (`:60-77`) is a reversible non-current state.
- `delete` (`:79-94`) is a soft tombstone.
- `restore` (`:96-114`) reactivates the chosen row without first enforcing that its exclusive slot has no newer active successor. It can create two active truths.
- `compress` (`:139-197`) creates a summary and archives inputs. It builds source text by combining prior source sentences and can carry obsolete or negated language into the summary.

The maintenance runner (`app/services/lifecycle_maintenance.py:267-425`) finds exact duplicates and heuristic identity/slot clusters. “Safe” compression directly archives older rows. This is useful repair tooling, but it is compensating for missing write-time uniqueness and it bypasses a shared mutation contract.

## Other systems named memory

These are separate products and must not be silently folded into personal memory:

1. `app/services/memory_retrieval/` is workspace/agent retrieval. Its `store.py:25-55` creates its own sqlite3 tables and FTS index using UUID items, scopes, and types. `app/api/routes/memory_retrieval.py` exposes index/retrieve/item/prune endpoints. It uses hard delete and client-supplied scopes and has no personal-memory lifecycle.
2. `app/services/context_memory/` stores scoped summaries/events for agent context compaction. `app/api/routes/context_memory.py` exposes summary, event, preview, and compact operations. It is not durable user-profile memory.

Neo does not currently expose a separate agent tool that mutates the personal `Memory`/typed tables. Coding, research, and agentic services call `MemoryRetrievalService`, which belongs to the first subsystem above. Any future personal-memory agent tool must be an adapter to the canonical command contract, not another store implementation.

The shared `/api/memory` naming and lack of an explicit boundary make authorization and maintenance mistakes more likely. The redesign should give these stores distinct names, dependencies, and route scopes, but should not merge their schemas without a separate product decision.

## Why the architecture keeps failing

### 1. Memory identity contains the value it is supposed to replace

`MemoryExtractionService` and `memory_scope.py` infer domains from text and build category-specific slots. For the exact brief example, current deterministic extraction produces:

```text
old: domain=video,   slot=goal:video:create_long_form_cinematic_youtube_videos
new: domain=clearly, slot=goal:clearly:create_short_instagram_reels_clearly
```

The old and new values cannot conflict because both the inferred domain and slot differ. The final adverb “clearly” becomes the new domain. Persisting both through the current accepted-candidate path yields two active `Memory` rows. This directly demonstrates ambiguous identity, value-derived slots, and brittle fallback domain inference (`app/services/memory_scope.py:166-215`).

### 2. Correction intent and canonical positive facts are not first-class data

`app/services/memory_intent.py:149-158` recognizes updates through words such as “correction,” “actually,” “update,” “change,” and a narrow “I now prefer” form. The ordinary correction “I no longer want X. I want Y” is classified as no explicit update. Extraction can split out the clean positive second sentence, but loses the retraction relationship. `MemoryExtractionService._correction_annotations` (`extraction.py:254-380`) similarly requires narrow triggers and a readily inferred domain. The new fact is appended because the old target is not carried as a typed operation.

Current regression tests mainly use highly scaffolded prompts such as “Correction: replace my video-editing goal and preference. My current ... I now prefer ...” (`tests/test_memory_test25_conflict_replacement.py:13-69`, test at `:343`). They validate added heuristics but do not cover the simpler required sentence. Tests at `:651`, `:764`, and `:800` cover clause boundaries, cross-category hints, and response-style demotion, but not a general correction operation whose target and replacement cross lexical domains.

### 3. Dual writable stores make correctness path-dependent

Candidate review writes both typed rows and `Memory`, direct answers often read typed rows, broad recall reads `Memory`, and UI CRUD may update only one correctly. `MemoryStore._update_matching_memories` (`memory_store.py:1073-1084`) changes text and importance but not all fingerprints, slots, FTS, or embeddings. Generic `update_memory` has the inverse problem: it does not synchronize typed projections. A test through one API can pass while plan generation or a differently worded direct question sees stale state.

### 4. Conflict and lifecycle rules are distributed heuristics

Conflict behavior differs by category inside `MemoryReviewService._accept`; explicit replacement has separate preference and goal implementations (`review.py:890-1123`); tombstones use another identity function; maintenance has another grouping algorithm; manual and typed CRUD largely bypass all of them. Restore does not enforce exclusivity. Periodic compression is consequently asked to repair contradictions that should have been impossible to commit.

### 5. Canonical and derived writes are coupled but not atomic

`MemoryStore.add` can call FTS and the embedding provider before the caller commits. FTS is local SQL but maintained manually; embedding work can be slow or fail. Other updates omit index synchronization. There is neither an outbox nor a durable retry ledger. The result is the worst combination: external/derived work lengthens canonical transactions, yet stale or missing derived rows are still possible.

### Additional failure drivers

- **Over-acceptance and fallbacks:** malformed or rejected model output can be replaced by regex extraction or generic memories (`extraction.py:623-672`, `:801-903`). The model is not authoritative—which is correct—but the deterministic fallback is too willing to create active facts.
- **Free-form candidate contract:** operational attributes such as replacement hints and slots are serialized inside `MemoryCandidate.reasoning`, so database constraints cannot validate them.
- **Hard-coded domains:** `memory_scope.py`, `retrieval.py:350-451`, `memory_store.py:1500-1517`, and `lifecycle.py:347-368` contain separate special cases for hardware, editor, career, Flutter, and other vocabulary.
- **Unsafe merge:** `MemoryReviewService._merge` joins arbitrary text instead of defining compatible fields and canonical values.
- **Over-broad explicit deletion:** removal matches any overlapping token rather than a confirmed identity/slot (`memory_store.py:1022-1071`).
- **Prompt trust:** recalled user-controlled text is interpolated into a stable system message (`chat.py:124-198`) rather than a delimited, explicitly untrusted context message.
- **Background bypass:** research jobs retrieve personal data through global `SessionLocal` and their own regex/category selectors (`app/services/research/memory_scope.py`) instead of the request owner and recall service.
- **No record-level authorization:** physical profile databases reduce risk but cannot prove owner safety for a row or vector hit.
- **Read-side duplication:** typed lists plus generic memories produce duplicate context and inconsistent limits. `_compact_context` takes up to 18 combined lines rather than enforcing a small record and token budget.
- **Schema drift:** create-all plus conditional ALTER/backfill helpers provide no explicit schema version, repeatable validation, or rollback.
- **Mutation on read:** legacy repair and extraction can commit during ordinary chat; recall usage updates commit or roll back depending on the response branch.

## Test assessment

The current 51 tests are valuable regressions, especially:

- `tests/test_memory_test21_regressions.py:13-46`: multiple durable facts survive and appear in direct recall.
- `tests/test_memory_test22_dedup.py:13-55`: repeated/refined facts deduplicate.
- `tests/test_memory_test24_scope.py`: topic scoping, arbitrary domains, broad/scoped recall, and domain-specific versus global response-style behavior.
- `tests/test_memory_test25_conflict_replacement.py:343-825`: explicitly signposted replacement, provider fragmentation, negation boundaries, compound domains, category protection, and preference classification.

They currently test outputs reached through selected chat/extraction paths more heavily than global invariants. Missing coverage includes path parity across all APIs, stale index repair after every mutation, rollback semantics, owner predicates and cross-owner vector collisions, concurrent writes to one exclusive slot, import and consolidation conflicts, restore against a newer successor, incognito/no-memory gates, and the exact implicit correction in the brief. The redesign's test matrix treats those invariants—not individual regex outputs—as the acceptance surface.

## Current-state conclusion

Neo does have useful pieces: SQLite transactions, provenance rows, lifecycle audit, active-state filtering in generic search, typed candidate review, hybrid recall, and best-effort embedding status. The persistent failures are not caused by one missing regex. They arise because identity is not stable, correction is not an operation, two writable representations coexist, mutation rules are distributed, and derived indexing has no clean post-commit contract. More patches in extraction or retrieval will continue moving failures between paths.
