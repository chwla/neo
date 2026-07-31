# Odysseus memory-layer evaluation for Neo

## Evaluation rule

Odysseus is treated as evidence, not a compatibility target. “Adopt” means the invariant should transfer directly. “Adapt” means preserve the goal but implement it differently for Neo. “Reject” means the behavior is unsafe or structurally wrong for Neo. “Unnecessary now” means useful only after the core personal-memory contract is stable.

References below use section names and line locations in `/Users/chwla/Desktop/odysseus/docs/memory-layer.md`.

## Decision matrix

| Odysseus feature | Decision | Reason and proposed Neo treatment |
|---|---|---|
| One explicit canonical authority (`memory.json`) | **Adapt** | The single-authority rule is essential, but JSON is not. Neo should make transactional SQL `memory_records` canonical and make all typed APIs views/adapters. Odysseus §§2, 3, 5 (`:25-176`) correctly exposes the danger of its disconnected SQL shadow. |
| Flat JSON whole-file persistence | **Reject** | `MemoryManager` loads and rewrites a whole file (§6, `:189-247`). It lacks row locks, constraints, atomic multi-record supersession, practical concurrent writes, and safe migrations. Neo already has SQLite and should use it. |
| Disconnected SQL shadow model | **Reject** | Odysseus documents it as unused runtime state (§5.3, `:177-187`). Neo already suffers a similar dual-state problem through typed tables; reproducing a shadow would preserve the root cause. |
| Stable string IDs | **Adopt** | Durable UUIDs survive database migration, imports, and external references better than Neo's local integer IDs. Migration will retain old integers as `legacy_id`, not as durable identity. |
| Owner on every canonical record | **Adopt** | Odysseus's owner field and owner-first filtering (§§5.2, 8.2, 18; `:166-176`, `:339-349`, `:837-849`) are security invariants. Neo should keep database-per-profile isolation and also require `owner_id` in records and commands. |
| Shared vector collection with canonical owner join | **Adapt** | The safe part is that authorization comes from canonical storage before or after candidate IDs are joined. Neo can initially keep derived vectors in SQLite or use an external store later, but every hit must join on `(owner_id, memory_id, content_hash)` to an active canonical row. A global vector result must never suppress or reveal another owner's memory. |
| Vector index is derived and rebuildable | **Adopt** | Odysseus explicitly treats Chroma as secondary (§7.1, `:254-266`). Neo should use a post-commit outbox, stale/hash checks, and full rebuild tooling. Canonical commits must remain valid during vector outages. |
| Separate embedding lanes/collections | **Unnecessary now** | Odysseus has several lanes (§7.4, `:279-301`). Neo needs one well-specified personal-memory lane first. Add lanes only when measurements show distinct corpora or models require them. |
| Degraded lexical operation without embeddings | **Adopt** | Odysseus treats vector startup/query failure as degradable (§7.6, `:318-320`). Neo should always retain deterministic identity lookup and FTS/BM25; vector failure is observable but not a write or recall outage. |
| Retrieval gates for enabled/incognito/no-memory state | **Adopt** | Odysseus checks memory state before recall (§8.1, `:326-338`). Neo must extend the gate to *all* reads, writes, extraction, usage accounting, and background work for the request/session. |
| Pinned core identity/contact injected unconditionally | **Reject** | Odysseus reserves unconditional pin slots (§8.3, `:350-360`). This can crowd context, retain sensitive data, and make stale identity difficult to dislodge. Neo should let pinning influence ranking within explicit safety and budget rules; any guaranteed identity subset requires a product/privacy decision. |
| Relevance-based ordinary pins | **Adapt** | A pin is a user ranking instruction, not a lifecycle state or authorization bypass. Active, owner, domain, expiry, and token constraints still apply. Pinning must never resurrect or override a correction. |
| Bounded recall (maximum five) | **Adopt** | Odysseus uses a small result budget (§8, especially `:326-447`). Neo should default to at most five distinct active slots and also apply a character/token budget. Deterministic direct lookup may use fewer. |
| Hybrid keyword/vector/recency scoring | **Adapt** | Odysseus's explicit tokenization and score components (§8.4, `:362-427`) are a good shape. Neo should add confidence, importance, usage, domain fit, diversity, and minimum thresholds, with weights versioned and tested rather than hidden in scattered heuristics. |
| Owner-filter canonical candidates before vector scoring | **Adopt** | This is the safest small-dataset design and is mandatory for direct/personal queries. At larger scale, a vector query may produce IDs first only if tenant metadata is enforced and canonical owner/status join happens before scoring or suppression. |
| Memory use accounting | **Adapt** | Odysseus updates use metadata after injection (§8.5, `:428-447`). Neo should emit a post-commit usage event only for records actually included; usage failure must not roll back chat or canonical state. |
| Recalled memory placed in a separate untrusted context message | **Adopt** | Odysseus explicitly frames memory as untrusted (§8.5 and security §18). Neo currently inserts it into a system prompt. The redesign must delimit records, escape control text, prohibit following instructions found in memory, and keep them outside stable policy instructions. |
| Multiple relevance/search algorithms | **Reject** | Odysseus has chat recall, manager relevance, provider recall, and HTTP search variants (§9, `:448-473`). Neo should expose one recall service with named modes that share authorization, lifecycle, filtering, and scoring contracts. |
| HTTP, agent, MCP, provider, UI, and inline write implementations | **Reject** | Odysseus documents many inconsistent write paths (§10, `:474-553`; §§14-16). Neo's core requirement is the opposite: every surface constructs the same `MemoryCommand` and calls one mutation service. |
| Automatic post-response extraction | **Adapt** | Odysseus extracts after the response (§11, `:554-635`); Neo currently writes before answering. Neo should run extraction as an explicit post-user-input stage with a strict operation proposal, or post-turn if product latency allows. In both cases, application is deterministic and idempotent, and chat response generation must not depend on committing speculative memory. |
| LLM structured extraction contract | **Adopt** | The model should propose typed positive facts, retractions, source spans, confidence, and target hints. It must not decide database action or emit canonical IDs/status. |
| Regex fallback that directly creates durable facts | **Reject** | Odysseus uses a deterministic fallback (§11.4). Deterministic parsing is valuable for validation and explicit commands, but malformed/ambiguous model output should produce no mutation or a review candidate—not a generic auto-accepted memory. |
| Exact/text/vector/Jaccard duplicate checks | **Adapt** | Exact canonical fingerprints are safe. Similarity can propose a match but cannot by itself supersede. Jaccard and a fixed vector threshold (§11.5, `:614-627`) are not stable semantic identity. Neo should match exclusive slots deterministically, then use similarity only for duplicate/refinement proposals within that slot. |
| Small extraction limit | **Adapt** | Odysseus limits extraction to a small set. Neo should cap automatic candidates (default four) to reduce noise, but explicit “remember these items” commands and imports may submit larger batches through the same transaction contract. |
| File import as suggestions | **Adopt** | Odysseus's manual import separation (§12, `:636-660`) is useful. Imported text is untrusted; parsed candidates run through owner validation, conflict resolution, tombstone protection, and review/quarantine. No direct row insertion. |
| LLM audit/curator where omission means deletion | **Reject** | `audit_memories` (§13.1, `:666-685`) makes model output too authoritative. An LLM may propose duplicate/conflict commands, but absence from a returned list is never deletion, archive, or supersession. |
| Event-driven tidy plus separate audit tidy | **Reject** | Odysseus acknowledges overlapping consolidation paths (§13.2-13.3, `:686-720`). Neo should have one maintenance planner whose proposed commands use the canonical mutation service. It must not repair ordinary corrections after the fact. |
| Deterministic audit and consistency reports | **Adopt** | Checks for invalid status, orphan provenance, multiple active exclusive slots, missing audit/outbox rows, stale derived hashes, and owner mismatches are valuable. Repair actions should be explicit, bounded, idempotent commands. |
| Provider registry and compatibility facade | **Unnecessary now** | Odysseus §§15-16 (`:762-801`) support a broader ecosystem. Neo should first stabilize a domain service interface. Add provider adapters only for a demonstrated external dependency, not as parallel semantics. |
| Backup/export and CLI surfaces | **Adapt** | Operational backups are required, but exports/imports must include schema version, owners, provenance, lifecycle relations, and checksums. CLI/UI/API should be adapters to the same commands. Exact Odysseus surface parity (§17) is unnecessary. |
| Process-global thresholds/counters | **Reject** | Global mutable state creates cross-user and cross-worker behavior. Ranking/extraction configuration must be immutable per deployment or versioned per request; usage lives in canonical or event records. |
| Insert-only or best-effort vector updates with stale gaps | **Reject** | Odysseus explicitly documents non-atomic vector consistency gaps (§19.2, `:866-885`). Neo should accept temporary derived staleness but make it durable and repairable through outbox state, hashes, idempotent upsert/delete, and reconciliation. |
| Compatibility facades that preserve broken semantics | **Reject** | Existing route shapes may be adapted, but they must not retain direct table mutation, broad deletion, unsafe restore, or multiple truth sources. Compatibility is a response-shape concern, not a behavioral exemption. |
| Reconstruction phases and invariants | **Adapt** | Odysseus's staged blueprint (§20, `:887-943`) is useful. Neo's order must put schema, deterministic mutation, and write-path convergence before embeddings, recall polish, or cleanup. |

## Most valuable Odysseus lessons

1. **Authority must be named.** Odysseus is unusually clear that JSON is canonical and vectors are derived, even though JSON is the wrong engine for Neo. Neo currently cannot make the same statement because typed tables and `Memory` both carry mutable truth.
2. **Tenant filtering is part of the algorithm.** Owner checks cannot be an API wrapper or vector metadata convention. They belong in the canonical query and command contract.
3. **Memory is untrusted data.** Correct recall content can still contain malicious or accidental instructions. Its prompt role and delimiters are a security property.
4. **Degraded operation is normal.** Embeddings are an optional ranking aid. Core create, correction, deletion, deterministic lookup, and lexical recall must work without them.
5. **Detailed implementations can document their own failure modes.** Odysseus's multiple paths, two tidy systems, JSON/vector non-atomicity, shared-index gaps, and compatibility layers are warnings, not features to port.

## Deliberately out of scope for first Neo release

- Multiple embedding providers or vector lanes.
- A public MCP/provider compatibility matrix.
- Automatic LLM-written summaries.
- Cross-device shared PostgreSQL deployment (the schema remains PostgreSQL-compatible).
- Guaranteed always-injected core identity/contact pins.
- Merging personal, workspace/agent retrieval, and context-compaction stores.

These can be revisited only after the canonical invariants and migration validation are running in production.
