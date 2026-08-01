# Neo memory v2 Phase 5 manual validation

Phase 5 validates owner-bound canonical reads, bounded lexical recall, deterministic direct
lookup, safe prompt integration, and final-ID usage accounting. It does not validate embeddings,
vector indexes, migration, rollout, or Phase 6 workers.

## Safety boundary

Run only the disposable validator:

```bash
.venv/bin/python scripts/manual_memory_v2_phase5.py --keep
```

The validator creates fresh SQLite profile databases beneath a
`neo-memory-v2-phase5-*` temporary directory. Canonical fixtures are populated through the
approved Phase 2–4 adapter/coordinator mutation path. It never opens the normal Neo browser or
production profile. The retained database path and exact cleanup command are printed last.

## What the validator proves

The deterministic battery covers:

1. broad recall across more than ten active records, capped at five and 2,400 characters;
2. video-creation domain recall and low-relevance omission;
3. the old long-video/new-reels canonical state;
4. current-turn suppression before replacement commits, with zero usage for the old goal;
5. domain-specific preference lookup;
6. canonical direct answers;
7. research-plan recall through the same serializer;
8. escaped prompt-injection text;
9. unrelated and inactive pinned records;
10. superseded, archived, forgotten/logically-deleted, and expired record exclusion;
11. identical cross-owner text with zero ID leakage;
12. incognito and memory-disabled zero-query gates;
13. missing research owner context and database-binding mismatch;
14. ownerless archive rejection;
15. usage-recording failure without prompt failure;
16. lexical-unavailable deterministic fallback;
17. exact type/domain/slot preference lookup;
18. shared sync/stream prompt selection and chat/research canonical-ID selection; and
19. zero vector and legacy serving reads.

A passing run ends with `phase5_fixture_validation=PASS`. All boolean invariants must have the
expected values, the broad count must be between zero and five, inactive/cross-owner/archive
counts must be zero, the direct/chat/research ID sets must agree, and both memory gate component
counts must be zero. The script exits nonzero on any failed assertion.

The serialized memory block is a separate `user` message identified in its content as
`neo_untrusted_memory_context`. It contains escaped display text and canonical ID/type/domain,
but no source excerpts, provenance, operation data, model reasoning, scores, or sensitive
plaintext. Recalled values never enter the stable system-policy string.

After inspection, run the exact printed cleanup command. Do not reuse the disposable database
for migration or production-profile testing.
