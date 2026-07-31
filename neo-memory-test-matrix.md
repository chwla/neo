# Neo memory redesign: behavioral test matrix

## Test contract

Tests assert canonical state and invariants, not just generated prose. Each test records:

- operation result and stable reason code;
- candidate and durable lifecycle transitions;
- complete active rows for the owner/slot;
- supersession/merge/source/operation relations;
- normal recall and explicit history results;
- FTS/vector/outbox state;
- behavior with vectors disabled;
- owner predicates and transaction outcome.

Use a deterministic clock, UUID generator, embedding fake, and normalization/policy version. Model tests use strict fixtures and are separate from mutation tests. The core mutation suite runs against SQLite and PostgreSQL (when supported); concurrency tests must exercise real separate connections, not one mocked session.

Abbreviations: `A` active, `S` superseded, `R` archived, `D` deleted, `NR` needs review, `Ø` no durable record. “Index pending” means canonical commit succeeded and outbox work exists.

## A. Creation, identity, duplicate, and refinement

| ID | Scenario/input | Expected transition | Expected canonical active state | Normal recall | Persistence / derived expectation |
|---|---|---|---|---|---|
| A01 | Create a valid durable goal in a free exclusive slot | candidate proposed → validated → applied; create `A` | One goal with typed positive value | Returns it for broad and scoped query | Record/source/operation/outbox atomic; index becomes current |
| A02 | Exact duplicate from another message | proposed → validated → reconfirm existing | Same one ID, newer confirmation and second provenance | One result, not duplicated | No second active row/vector; upsert only if canonical hash changed |
| A03 | Paraphrased duplicate with equal normalized typed value | proposed → validated → reconfirm | Same one active record | One result | Similarity may suggest, type comparator confirms; provenance attached |
| A04 | High vector similarity but different exclusive value and no correction intent | proposed → `NR`/conflict_requires_replace | Existing active unchanged | Existing only | No canonical/vector insertion for proposal; similarity alone cannot replace |
| A05 | Compatible refinement: same goal plus target date | validated → update/refine | One active goal with structured date and incremented revision | Refined value only | Expected revision enforced; old vector hash marked for replacement via outbox |
| A06 | Incompatible field change sent as `update` | reject requires replace | Existing unchanged | Existing only | No canonical/index write |
| A07 | Two explicitly concurrent additive goals | two creates | Two active records with distinct entity-stable slots | Both when relevant, budget permitting | Separate hashes/vectors; no exclusive constraint collision |
| A08 | Same trailing word in unrelated topics | create independent facts | Stable domains/slots; no fallback-to-last-token identity | Scoped queries isolate each | Domain/slot policy version recorded |
| A09 | Unknown durable topic | validate stable normalized topic or `NR` if ambiguous | Never a slot derived from value tail | Only if validated and relevant | Taxonomy decision and evidence recorded |

## B. Corrections, conflicts, category, and negation

| ID | Scenario/input | Expected transition | Expected canonical active state | Normal recall | Persistence / derived expectation |
|---|---|---|---|---|---|
| B01 | Direct correction: “Correction: replace X with Y” | replace: old `A→S`, new `A` | New only | New only; old visible only in history | One transaction, one supersedes relation, old derived delete + new upsert |
| B02 | **Critical implicit correction:** old “create long-form cinematic YouTube videos”; then “I no longer want to make ... . I want to create short Instagram reels clearly.” | structured retraction+positive assertion → replace | Exactly one active goal: `create short Instagram reels clearly` in inherited `video_creation/primary_output` slot | Broad recall and video query return only new; plan uses only new | Old `S`, new `A`, clean display text, relation/source spans; old vector dropped even if returned stale |
| B03 | Replacement text contains `not old value` after positive clause | validator strips old clause using spans or sends `NR` | If applied, positive value only | Never shows/embeds “new, not old” | Canonical positive validator fails mixed text; raw clause allowed only in source |
| B04 | Several active legacy conflicts in one exclusive slot | one explicit replacement | All predecessors `S`, exactly one new `A` | New only | One relation per predecessor; invariant warning; atomic commit |
| B05 | Category correction: “That is a goal, not a preference” | wrong active record `S`, correct type `A` | Correct type/slot only | Appears in goal lookup, absent from preference lookup | Cross-type replace relation permitted by explicit category correction; indexes updated |
| B06 | Domain-specific advice preference | create preference under topic domain | No global response-style record | Used only for matching domain | Slot `preference:<domain>:<dimension>`; global query does not overapply |
| B07 | Truly global response-style preference (“Always answer me concisely”) | create exclusive global response-style | One global preference | Applies across domains within prompt budget | Explicit global classifier evidence |
| B08 | Goal ends in “clearly” | create/replace goal | Domain comes from subject/topic, not `clearly`; type remains goal | Goal recall only | No unrelated preference/global slot |
| B09 | Pure negation/retraction with no replacement (“I no longer live in Pune”) | retract/delete/archive according to explicit intent policy; no positive record | Old not active; no record whose value is “not live in Pune” | Nothing for current location | Tombstone/history and derived delete; no negative canonical value |
| B10 | Negated hypothetical/third-party statement | reject | `Ø` | Nothing | Diagnostic only |
| B11 | “I prefer X, not only Y” where `not only` is additive | validated structured positive preference | Correct combined/additive semantics | Positive preference | Boundary parser does not truncate `not only` |
| B12 | Explicit correction changes domain as well as value | replace with explicit domain-change evidence | New record in corrected domain, old `S` | Only new under new domain | Relation crosses domain; requires explicit evidence, not similarity guess |
| B13 | Ambiguous “Now I want Y” with existing exclusive X but no reliable relation | `NR` under safe default | X stays active until review | X only; Y not injected | Candidate stored, no vector entry |

## C. Lifecycle, history, deletion, and resurrection

| ID | Scenario/input | Expected transition | Expected canonical active state | Normal recall | Persistence / derived expectation |
|---|---|---|---|---|---|
| C01 | Archive active record | `A→R` | None in slot | Not returned | Archive operation plus derived delete; history retains row |
| C02 | Restore archived record into empty slot | `R→A` or new active version per type policy | One restored active | Returned | Constraint check; derived upsert |
| C03 | Restore archived record where newer successor is active | reject `active_successor_exists` | Newer remains sole active | Newer only | No index/canonical change |
| C04 | Explicit restore-as-replacement of historical value | current `A→S`, new version of historical value `A` | Restored value only | Restored value only | New operation/version and relations; never flips old row blindly active |
| C05 | Delete/forget active record | `A→D` | None | Never returned | Delete/tombstone policy applied; derived delete queued |
| C06 | Re-extraction of deleted value without explicit reconfirmation (“resurrection bait”) | reject `resurrection_blocked`/`NR` | None | Nothing | No upsert; lineage/tombstone match audited |
| C07 | Explicit user reconfirms deleted value | new active version only if privacy/product policy permits | One new active | New only | Explicit source, operation, lineage/tombstone decision recorded |
| C08 | Supersession history request | no mutation | New active; old `S` | Normal recall new only; authorized history shows both and relation | History query is separate and owner-bound |
| C09 | Expired event/fact | active eligibility ends and expiry job archives if configured | Not active/eligible after clock | Never current recall | Derived stale hit dropped; archive command idempotent |
| C10 | Message edit removes final supporting source | source-change command reevaluates record | Per evidence policy: retain with remaining source, archive, or `NR`; never broad text match | Reflects resulting canonical status | Source detach and lifecycle/index events atomic |
| C11 | Chat delete with same fact supported by another chat | detach one source only | Record remains active with other provenance | Still returned | No vector delete; source counts correct |

## D. Recall, relevance, prompt safety, and index degradation

| ID | Scenario/input | Expected transition | Expected canonical active state | Normal recall | Persistence / derived expectation |
|---|---|---|---|---|---|
| D01 | Broad saved-memory recall with >5 active records | no lifecycle change | All remain active | At most five distinct slots and token budget; deterministic ranked/diverse result | Usage events only for injected IDs |
| D02 | Scoped query with high-similarity other domain | none | Unchanged | Matching domain only unless explicit broad query | Domain gate applies before final rank |
| D03 | Deterministic identity/preference lookup | none | Unchanged | Exact active slot result without vector dependency | No derived requirement |
| D04 | Stale vector hit for superseded predecessor | none | Successor `A`, predecessor `S` | Successor only; predecessor discarded | Queue stale delete; no usage for predecessor |
| D05 | Ghost vector ID with no canonical row | none | Unchanged | Ghost omitted | Queue delete/metric; lexical results unaffected |
| D06 | Vector content hash older than canonical revision | none | Current canonical active | Current text only, found lexically/deterministically if needed | Stale hit dropped and upsert queued |
| D07 | Vector outage | none | Unchanged | Deterministic+FTS result within same authorization/status limits | Degraded metric; no failed canonical operation |
| D08 | FTS outage and vector outage | none | Unchanged | Deterministic explicit lookup; otherwise bounded canonical fallback or no result per threshold | Clear degraded state, no raw table dump |
| D09 | Low-relevance memories | none | Unchanged | Below-threshold records not injected | No usage event for omitted records |
| D10 | Pinned but unrelated active memory | none | Unchanged | Pin boost cannot bypass strong domain/relevance/safety/budget gate | No special authorization/index behavior |
| D11 | Pinned superseded/deleted/expired memory | none | Inactive remains inactive | Never returned | Any stale derived hit cleaned |
| D12 | Memory text contains “ignore prior instructions…” | none | Fact may be active if otherwise allowed | Serialized only in delimited untrusted context; model/system policy test proves instruction not followed | Raw metadata/provenance omitted from prompt |
| D13 | Current user message conflicts with recalled old fact before async extraction commits | none or later replace | Canonical may still contain old until command applies | Prompt builder excludes/marks matched old fact; current user message wins | No usage reinforcement for contradicted fact |
| D14 | Repeated broad recalls | usage events | Canonical truth unchanged | Ranking effect capped and deterministic | Usage updates independent; failure does not affect chat |
| D15 | Direct answer and normal plan generation for same facts | none | One canonical dataset | Both consume same active IDs/values, no typed-table divergence | Query trace records same canonical source |
| D16 | Background research plan asks for personal context | none | Unchanged | Uses the same owner/status/domain-bounded recall IDs and untrusted serialization as chat | No global/default `SessionLocal`; job owner is required and usage is attributable |

## E. Extraction and model failure

| ID | Scenario/input | Expected transition | Expected canonical active state | Normal recall | Persistence / derived expectation |
|---|---|---|---|---|---|
| E01 | Model returns malformed JSON | extraction failure, no applied candidate | `Ø`/unchanged | Nothing new | Diagnostic only; no regex generic auto-accept/vector |
| E02 | Model returns unknown type or operation | schema reject | Unchanged | Nothing new | Stable rejection reason |
| E03 | Model invents a fact absent from user span | grounding reject | Unchanged | Nothing new | No source/active record |
| E04 | Model returns assistant statement as user memory | speaker reject | Unchanged | Nothing new | Diagnostic only |
| E05 | Temporary fact (“I am drinking coffee right now”) | durability reject/expiry candidate according to policy | No durable active record | Nothing in future chat | No index write |
| E06 | Stable first-person fact mixed with a temporary request | one durable proposal only | Durable fact active | Durable fact when relevant; request absent | Source spans prove grounding |
| E07 | More than four incidental candidates | automatic cap and deterministic priority; rest ignored/reviewed | At most configured automatic mutations | Only applied facts | Cap reason recorded; no partial hidden mutation |
| E08 | Explicit “remember these 10 facts” | bounded batch through same pipeline | Valid independent facts only; conflicts handled per item | Budgeted recall, not all 10 injected | Per-item operation/idempotency results |
| E09 | Ambiguous pronoun/subject | `NR` or reject | Unchanged | Nothing new | No guessed owner/subject |
| E10 | Secret/API token in source | sensitive-policy reject/redact | No prohibited memory | Nothing | Raw text not logged/indexed |
| E11 | Model labels topic preference as global response style | deterministic validator corrects domain or sends `NR` | Never erroneous global active record | Domain only if applied | Model category is proposal, not authority |

## F. Transactions, concurrency, indexing, and idempotency

| ID | Scenario/input | Expected transition | Expected canonical active state | Normal recall | Persistence / derived expectation |
|---|---|---|---|---|---|
| F01 | Canonical SQL failure midway through multi-predecessor replace | operation fails/rolls back | All old records remain active; no partial successor | Old state only | No committed sources/relations/outbox/index effects |
| F02 | Derived-index update fails after canonical create | create commits `A`; outbox `failed/pending` | New canonical active | Deterministic/FTS as available returns it | **Canonical is not rolled back**; retry later makes vector current |
| F03 | Process crashes after canonical commit before worker | create/replace committed | Correct canonical state | Correct via canonical/lexical degraded path | Pending outbox recovered by lease/reconciliation |
| F04 | Process crashes after vector upsert before marking outbox done | canonical unchanged | Correct active state | One canonical result | Idempotent retry upserts same `(owner,id,hash)`, no duplicate |
| F05 | Two concurrent creates, same exact exclusive value | one create, second reloads as duplicate | Exactly one active ID | One result | Unique constraints hold; provenance/operation outcomes both recorded |
| F06 | Two concurrent different values, same exclusive slot, neither marked replacement | one create, other conflict/`NR` | Exactly one active | One result | No last-writer-wins contradiction |
| F07 | Concurrent explicit replacements of same slot | serialization/revision conflict; policy selects one committed order | Exactly one active terminal value | Terminal value only | Complete acyclic lineage, retries deterministic |
| F08 | Stale UI update with wrong expected revision | reject conflict | Latest record unchanged | Latest only | No index event |
| F09 | Same idempotency key/request retried | return original committed result | No duplicate state | One result | Unique owner/key; no duplicate source/outbox |
| F10 | Same idempotency key with different request hash | reject idempotency mismatch | Unchanged from original | Original only | Security/audit event |
| F11 | FTS update succeeds, vector fails | canonical active | Correct recall lexically | One result | Per-derived status; vector retry does not rewrite canonical |
| F12 | Reconciliation after manually missing index row in test fixture | none to canonical | Unchanged | Canonical/lexical behavior preserved | Missing hash detected and repaired idempotently |

## G. Owner isolation, modes, import, and maintenance

| ID | Scenario/input | Expected transition | Expected canonical active state | Normal recall | Persistence / derived expectation |
|---|---|---|---|---|---|
| G01 | Cross-user lexical query for identical fact | none | Each owner's state independent | Owner A sees only A IDs/sources | SQL owner predicate required in query plan |
| G02 | Cross-user vector collision; B's vector ranks first | none | Independent | A never sees B and B hit does not suppress A fallback | Wrong-owner hit discarded; no A usage/delete of B vector |
| G03 | Request body owner differs from authenticated owner | reject before repository | Unchanged | Nothing | No operation/candidate/index write |
| G04 | Profile database context and row owner disagree | fail closed | Unchanged | No fallback/default DB read | Security alert |
| G05 | Unauthenticated request to personal-memory API | reject | Unchanged | Nothing | No process-default DB access |
| G06 | Incognito normal chat | no extraction/retrieval/usage | Unchanged | No personal memory context/direct answer | No operations/outbox/background work |
| G07 | Incognito explicit “remember this” | explicit denial/leave-incognito flow | Unchanged | Nothing new | No hidden write |
| G08 | Memory disabled but vectors contain hits | no memory processing | Unchanged | No memory results | Vector service not called |
| G09 | Guest ephemeral memory enabled, then guest ends | guest-owner create in isolated ephemeral store, later purge | No durable registered-owner impact | Guest sees only within allowed session | Purge canonical and derived per guest policy |
| G10 | Import exact duplicate | reconfirm existing or report duplicate | One active | One result | Import provenance attached; no duplicate vector |
| G11 | Import conflicts with exclusive active fact | `NR` unless import has explicit authorized replace instruction | Existing stays active | Existing only | Imported candidate has no vector |
| G12 | Import contains deleted/superseded old value | resurrection blocked | Current/none remains | Old absent | No vector upsert; reason reported |
| G13 | Consolidation proposes exact duplicate merge | command validates compatible merge | One active keeper/merged version | One result | Lineage/provenance preserved; derived cleanup queued |
| G14 | Consolidation proposes conflicting summary or omits a record | reject/no action | Existing active state unchanged | Unchanged | LLM omission never deletes/archives |
| G15 | Maintenance expiry/archive command retried | one idempotent archive | None active after expiry | Absent | One lifecycle operation/delete event |
| G16 | Agent tool, UI, HTTP, chat extraction submit equivalent commands | same mutation contract outcomes | One active canonical truth | Same recall | Contract test proves no direct-table paths |
| G17 | Ownerless/current Qdrant conversation archive hit is offered to authenticated context | reject corpus/result until owner-safe archive manifest exists | Personal memory unchanged | Archive text not injected | No cross-owner search; feature remains disabled or owner join fails closed |
| G18 | Background research job loses/omits profile context | fail closed, no personal recall | Unchanged | No default-profile memory is injected | Structured diagnostic; job may continue without memory according to product policy |

## H. Migration and compatibility

| ID | Scenario/input | Expected transition | Expected canonical active state | Normal recall | Persistence / derived expectation |
|---|---|---|---|---|---|
| H01 | Legacy `Memory` and typed record exact duplicate | migration merge/reconfirm | One v2 active record | One result | Both legacy IDs/provenance mapped; one vector |
| H02 | Legacy typed and generic exclusive values contradict without evidence | both quarantined/`NR` under safe migration rule | No guessed active value | Neither in normal recall | Conflict report, no vectors |
| H03 | Valid legacy supersession chain | migrate history and terminal | Terminal only active | Terminal only | Relations acyclic; predecessors absent from index |
| H04 | Legacy row has `is_active=true`, `status=deleted` | quarantine or normalize inactive only with trustworthy audit | Never active | Absent | Report mismatch; no vector |
| H05 | Legacy canonical text contains “new, not old” | extract positive only if deterministic span evidence; otherwise `NR` | Clean positive or none | Never mixed text | Raw legacy link in report/source only |
| H06 | Existing integer IDs collide across profile DBs | deterministic owner/source UUID mapping | Correct independent rows | Owner-bound results | Mapping table preserves both; no vector ID collision |
| H07 | Migration rerun after checkpoint crash | idempotent continuation | Same dataset/checksum | Same results | No duplicate records/relations/outbox |
| H08 | Vector service down during migration | canonical migration succeeds | Correct active rows | Lexical/deterministic recall | Coverage pending and reported; rollout can use degraded gate |
| H09 | Legacy compatibility typed PATCH after v2 cutover | adapter emits v2 update/replace command | V2 is sole active truth | Generic and typed views agree | Legacy table unchanged/read-only; v2 index event |
| H10 | Message edit spans cutover watermark | queued idempotent source command applied once to current v2 state | Deterministic resulting state | Correct current records | No split legacy/v2 mutation |
| H11 | Pre-cutover rollback rehearsal | v2 disabled/discarded | Legacy unchanged | Legacy behavior restored | Backup/checksum verified |
| H12 | Post-cutover full rollback with two new operations | stop, restore backup, replay operations | Both committed user actions preserved | Expected legacy-compatible results | Watermark and replay checksums match |

## Rule-to-test traceability

| Specification rule | Primary tests |
|---|---|
| One canonical authority / typed adapters | D15, G16, H01, H09 |
| Required owner and fail-closed tenancy | D16, G01-G05, G08, G17-G18, H06 |
| Stable type/domain/slot identity | A07-A09, B02, B06-B08, H02 |
| Positive canonical facts and grounded extraction | B02-B03, B09-B11, E02-E06, E09-E11, H05 |
| Candidate validation before active | A04, B13, E01-E11 |
| Exact/paraphrased duplicate | A02-A04, F05, G10, H01 |
| Compatible refinement versus conflict | A05-A06, F06-F08 |
| Atomic first-class supersession | B01-B05, F01, F07, H03 |
| Only active/unexpired recall | C01-C09, D04-D06, D11, H03-H05 |
| Archive/delete/restore semantics | C01-C11, G15 |
| Resurrection prevention on every path | C03-C07, D04, G12, G14 |
| Bounded/domain-aware/pinned recall | D01-D03, D09-D11 |
| Derived index non-canonical and repairable | D04-D08, F02-F04, F11-F12, H08 |
| Untrusted prompt injection and current-turn precedence | D12-D13, D16 |
| Usage only for injected records | D01, D09, D14 |
| Incognito and disabled-memory gates | G06-G09 |
| One command contract and idempotency | F09-F10, G13-G16, H07, H09-H10 |
| Transaction and concurrency behavior | F01-F12 |
| Safe import/consolidation | G10-G15 |
| Versioned, loss-audited migration and rollback | H01-H12 |

## Test-suite structure and release gates

Suggested suites:

1. `tests/memory_v2/unit/`: normalizers, taxonomy, positive-value validator, typed comparators, scoring, prompt serializer.
2. `tests/memory_v2/contract/`: parameterized command semantics and repository owner/status predicates.
3. `tests/memory_v2/integration/`: real SQLite transactions, FTS, outbox worker, chat/direct-answer/API adapters.
4. `tests/memory_v2/concurrency/`: multi-connection SQLite and PostgreSQL runs.
5. `tests/memory_v2/security/`: owner/vector collision, prompt injection, unauthenticated/default-DB, sensitive data.
6. `tests/memory_v2/migration/`: fixture databases for every known legacy schema and corruption/ambiguity class.
7. `tests/memory_v2/e2e/`: exact correction, broad recall, and normal plan generation with vector healthy/down/stale.

Release requires all rows above to pass, zero invariant-checker violations, no direct writes outside the mutation repository (enforced by architecture/static tests), and mutation/scoring/migration policy versions frozen in fixtures. Existing legacy tests remain unchanged during implementation; once v2 behavior intentionally differs, compatibility assertions should be added rather than rewriting history to make v2 appear equivalent.
