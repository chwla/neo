# Manual Memory Live Testing

Run these scenarios in the actual Neo UI after starting the backend, frontend, and memory worker from `docs/memory-operations.md`. Use the inspector alongside the UI. Record observations; do not treat this worksheet as self-certifying.

## Test 1 — Clean state

Open Memories.

Expected:

```text
No saved memories
```

`scripts/inspect_memory.py summary` must show zero records.

## Test 2 — Basic save

Send:

```text
I prefer concise technical explanations that still explain why something works.
```

Open a new conversation and ask:

```text
How do I prefer technical explanations?
```

Inspect canonical records and the conversation's final serialized IDs.

## Test 3 — Critical conflict replacement

Send:

```text
I want to create long-form cinematic YouTube videos.
```

Confirm it appears in Memories and works in another conversation. Then send:

```text
I changed my mind. I now want to create short Instagram reels clearly.
```

Ask in separate conversations:

```text
What are my current content goals?
What should I practise this week?
Make a content plan based on what you remember about me.
Am I trying to make long-form cinematic YouTube videos?
```

Pass requires: the new goal is active; the old goal is superseded; one exclusive slot is active; the old goal is neither injected nor usage-reinforced; the new value has no malformed negated old clause; and plans use only the new goal.

## Test 4 — Ambiguous correction

Send:

```text
Maybe I should stop making reels and try documentaries, but I’m not sure.
```

It must not silently replace the confirmed goal.

## Test 5 — Temporary facts

Send:

```text
I drank coffee today because I slept late. I might watch a movie tonight.
```

These must not appear as durable memory.

## Test 6 — Mixed message

Send one durable preference, one temporary event, and one uncertain future idea. Only the durable preference may become confirmed memory.

## Test 7 — Forget

Forget a memory through Memories. Restart Neo. The forgotten value must not return in direct, broad, or planning recall.

## Test 8 — Incognito

Open Memories and enable `Incognito (no memory calls)`. Send a memory-shaped fact, then run a Research query. The inspector must show zero new personal-memory records, operations, usage events, outbox events, FTS rows, and vector points.

## Test 9 — Memory disabled

Open Memories and turn off `Use personal memory`. Repeat the same chat and Research zero-call inspection.

## Test 10 — Restart

Save and correct a fact. Stop backend, frontend, and worker completely. Restart all three and verify exact recall and lifecycle state.

After completing the worksheet, report the conversation IDs and inspector output for any unexpected result.
