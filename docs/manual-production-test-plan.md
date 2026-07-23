# Neo Manual Feature Test Guide

Use this guide in a clean test environment with a test profile, a test model provider, and a small disposable Git repository. Complete the steps in order. For every step, record **Pass** or **Fail** and a screenshot if the result differs from the expectation.

Do not use a real production repository, customer data, or real secrets. Neo should always use a managed repository copy for code-changing work.

## 1. Start Neo

1. Start the backend and open Neo in a browser.
   - Expected: The profile picker appears. There are no blank screens or browser errors.

2. Open `http://127.0.0.1:8000/api/health`.
   - Expected: A successful health response is returned.

3. Refresh the browser.
   - Expected: Neo returns to a usable profile picker or restores the active test profile.

## 2. Profiles and sessions

4. Create a test profile with a password.
   - Expected: The profile is created and Neo opens its empty workspace.

5. End the session, then unlock the profile with the correct password.
   - Expected: The workspace opens and remains available after a page refresh.

6. Try to unlock the profile with the wrong password.
   - Expected: Access is denied with a clear message; no workspace data is shown.

7. Create a second test profile. In the first profile, create a project, note, and chat. Switch to the second profile.
   - Expected: The second profile cannot see any records from the first profile.

8. Start a guest profile, create a chat, then end the guest session.
   - Expected: The guest session ends cleanly. Guest data does not appear in another profile.

## 3. Chats and the main workspace

9. Create a new chat and send a short message.
   - Expected: Your message and the assistant response appear in order and the chat appears in the sidebar.

10. Send a message that streams a longer response.
   - Expected: The response appears progressively without duplicated text or a frozen composer.

11. Refresh the page and reopen the chat.
   - Expected: The complete transcript remains available in the same order.

12. Edit a message where the UI permits editing.
   - Expected: The edited content remains after refresh.

13. Delete the chat.
   - Expected: Neo asks for confirmation. After confirmation, the chat and its messages disappear; other chats remain intact.

14. Create a project and a chat within it. Open the project and chat links directly in the browser.
   - Expected: `/projects/{id}` and `/chats/{id}` open the intended record. Invalid links show a safe empty/not-found state, not an application crash.

15. Submit an empty message, then a message containing Unicode, markdown, and HTML-like text.
   - Expected: Empty input is not sent. Text is displayed safely; it does not run as browser code.

## 4. Personal memory

16. Open Settings → Memory. Visit Profile, Preferences, Goals, Projects, Events, and Memories.
   - Expected: Each tab loads and shows a clear empty state or existing records.

17. Add or update one profile fact, preference, goal, event, and durable memory.
   - Expected: Each record appears in the correct tab and remains after refresh.

18. Filter and sort the memories.
   - Expected: The selected filter and order change the displayed list correctly.

19. Archive a durable memory, restore it, and inspect its lifecycle detail.
   - Expected: Each state change is visible and a lifecycle record explains the action.

20. Supersede one memory with a revised version.
   - Expected: Neo shows the relationship between the old and replacement memory without silently deleting either audit trail.

21. Run memory extraction, review, and reflection with a provider configured.
   - Expected: Candidate/reflection results are attributable to the operation and can be reviewed.

22. Temporarily make the model provider unavailable and repeat a model-backed memory action.
   - Expected: Neo reports the provider problem clearly and does not invent a result.

## 5. Projects, tasks, and notes

23. Open Settings → Projects and create a project with a description, tag, and priority.
   - Expected: The project is listed and can be found by search/filter.

24. Edit, pin, archive, unarchive, and delete a test project.
   - Expected: Each action updates the visible state. Archived items follow the active filter; deletion removes only the selected project.

25. Open Settings → Tasks. Create a task with a project, priority, due date, tag, and status.
   - Expected: The task appears in the project and task lists with its selected metadata.

26. Create a child task, change task status, then pin, archive, and delete test tasks.
   - Expected: Parent/child structure is correct; status and visibility update after refresh.

27. Create a note with markdown and tags. Edit, pin, archive, and delete a disposable note.
   - Expected: The note lifecycle works and tags can be used to find it.

28. Attach a note to a project and task, then detach it from each.
   - Expected: The association appears from both records and disappears cleanly after detaching.

29. Try blank titles, very long titles, invalid dates, duplicate tags, and repeated clicks on create/update controls.
   - Expected: Neo validates or safely handles the input; it does not create corrupt duplicates or crash.

## 6. Files and artifacts

30. Open Files and upload a small text file.
   - Expected: The file appears with correct name/metadata and can be downloaded with identical content.

31. Link the file to a project or task and request a summary.
   - Expected: The link is visible; the summary appears when a provider is configured or a clear unavailable message appears otherwise.

32. Create or inspect an artifact produced by another workflow and download it.
   - Expected: The artifact has correct metadata, association, and downloadable content.

33. Try an empty file, unsupported/binary file, oversized file, duplicate file, and a filename containing path-like characters.
   - Expected: Invalid uploads are rejected safely. Neo never writes outside its workspace storage.

34. Delete the uploaded test file.
   - Expected: It is removed from the file list and cannot be downloaded; unrelated files and artifacts remain available.

## 7. Search and research

35. Open Settings → Web Search. View providers/configuration and run a provider test.
   - Expected: The active provider and its availability are clearly reported. Secret values are never displayed.

36. Run a direct web search and open one returned source.
   - Expected: Results show credible source metadata and links; the selected source is relevant to the query.

37. Use fetch and cited-answer features with a public test URL.
   - Expected: Fetched content is bounded and cited answers identify their evidence.

38. Open Settings → Reliable Web Search. Create a plan, run it, and inspect sources, evidence, conflicts, and cache.
   - Expected: Every source/evidence/conflict item belongs to the run and can be inspected.

39. Refresh the reliable-search run.
   - Expected: Neo records the refresh outcome without making the earlier run history misleading.

40. Open Settings → Research. Plan and run a research request. Review the run, evidence, claims, conflicts, and report.
   - Expected: The report is traceable to recorded evidence. Unsupported claims are not presented as verified facts.

41. Continue the research run, refresh it, and validate citations.
   - Expected: Each action updates run state and reports a clear result.

42. Disable the search provider or use an invalid endpoint, then retry a search and research run.
   - Expected: Neo reports a degraded/error state. It does not generate fake sources, citations, or reports.

## 8. LLM providers and runtime

43. Open Settings → LLM Providers. Add or update a test provider, model, and route.
   - Expected: The configuration persists and a health/test action gives an accurate result.

44. Select a model and use it in a short chat.
   - Expected: The selected route is used and the chat succeeds when the provider is healthy.

45. Open Settings → Provider Runtime. Inspect status, health, request history, rate limits, and usage after a chat.
   - Expected: The request is recorded with meaningful status, model/provider information, and timing/usage where available.

46. Start a streamed completion and cancel it.
   - Expected: The stream stops, its final status is clear, and usage/history is not duplicated.

47. Use an invalid model, invalid endpoint, or invalid key reference.
   - Expected: Neo reports an actionable failure. It never displays the raw secret value or claims the call succeeded.

## 9. Rules, agents, tools, and skills

48. Open Settings → Rules & Profiles. Create a scoped rule, resolve rules for a test context, and inspect the resolution log.
   - Expected: The effective rules and their sources are visible and consistent with the selected scope.

49. Import rules from the disposable repository.
   - Expected: Valid rules are imported; missing or malformed rule files produce a safe message.

50. Open Settings → Agents. Create/update/disable a custom agent definition, then reset built-ins if appropriate.
   - Expected: Definitions persist; disabled agents cannot be started; reset does not silently erase unrelated custom data.

51. Start a task-agent run from a task or objective. Inspect its plan and steps; approve a waiting step; cancel another run; save one run to a note.
   - Expected: Run state is visible at every stage. Approval controls the targeted step only. The saved note contains the correct run result.

52. Open Settings → Agentic Runs. Create a run, then plan, step, continue, reflect, inspect context, and stop it.
   - Expected: Each action produces a durable, ordered state transition. Repeating a completed/stopped action is handled safely.

53. Open Settings → Tools & Skills. Add a test tool server/definition/skill and run discovery if supported.
   - Expected: Configuration and discovery results are shown without exposing secrets.

54. Create a tool call that requires approval. Reject one call and approve another.
   - Expected: Rejected calls do not execute. Approved calls have an audit record and an accurate final status.

## 10. Repositories, code intelligence, and LSP

55. Register the disposable repository in Repos. Compare the original repository before and after registration.
   - Expected: Neo registers a managed copy. The original repository remains unchanged.

56. Inspect the repository and its files; delete a disposable registration only after later repository tests finish.
   - Expected: File metadata/path display is correct and does not reveal unrelated host files.

57. Open Codebase Index. Build the index, then inspect symbols, search results, routes, dependencies, and file summaries.
   - Expected: Results refer to the managed copy and match the repository’s known code.

58. Open Symbol Awareness. Build symbol awareness and inspect a definition, references, document symbols, related files, and context.
   - Expected: Navigation data is correct; empty/unsupported-language cases are explained clearly.

59. Open Language Server. Inspect available servers, start one for the managed workspace, view diagnostics, run a supported query, and stop it.
   - Expected: Server state is accurate. If no server is installed, Neo says so clearly and remains usable.

## 11. Coding Agent, patches, commands, tests, and Git

60. Start a Coding Agent run against the disposable managed repository. Inspect proposed actions.
   - Expected: Actions have visible status and do not change files before required approval.

61. Approve one coding action, reject another, revise a patch, propose a command, and cancel a separate run.
   - Expected: Only approved actions run. Rejected/cancelled actions do not modify the managed copy.

62. Open Patch Applications. Propose a valid patch, validate it, then try to apply it without confirmation and with confirmation.
   - Expected: Unconfirmed apply is refused. Confirmed apply changes only the intended managed-copy files and creates an audit record.

63. Try a malformed patch, stale patch, absolute path, traversal path, and conflicting hunk.
   - Expected: Each unsafe/invalid patch is rejected; no partial file change is left behind.

64. Open Command Sandbox. Validate and propose a safe test command. Try to execute before approval, then approve and execute it.
   - Expected: Execution is blocked before approval. After approval, the recorded argv, working directory, output, exit code, and status match the command.

65. Try shell metacharacters, command substitution, a disallowed executable, unsafe working directory, timeout, large output, and cancellation.
   - Expected: Unsafe commands are blocked without shell bypass. Timeout/cancel states are visible and auditable.

66. Open Test Runner. Detect test commands, create a saved command, run a passing test and a deliberately failing test, then inspect history.
   - Expected: Only saved/allowed commands run. Pass/fail output and status are accurate.

67. Open Git Checkpoints. Initialize Git in the managed copy, inspect status/diff, create a checkpoint, make a harmless managed-copy change, and restore the checkpoint.
   - Expected: Checkpoints are local to the managed copy. Restore returns the copy to the checkpoint and records the operation.

68. Compare the original disposable repository with its state before step 55.
   - Expected: The original repository is still unchanged.

## 12. Evaluation, orchestration, bundles, continuity, recovery, and GitHub

69. Open Evaluation Harness. Create or inspect a suite, run it, inspect cases/report, set a baseline, and compare results.
   - Expected: Scores, hard failures, reports, and baseline comparison match the executed suite.

70. Open Workspace Orchestration. Create a workspace, plan it, add/update nodes and edges, add timeline/artifact/link data, recompute readiness, and inspect health/report.
   - Expected: The graph and report persist. Invalid links/edges are rejected or clearly handled.

71. Open Bundles. Export a test bundle, inspect/download it, validate it, and import it into a clean test profile.
   - Expected: Validation runs before import; imported data matches the bundle scope; conflicts are reported rather than silently overwriting records.

72. Inspect the bundle for test secret-like strings, real credentials, unrelated profile data, absolute host paths, and unsafe archive paths.
   - Expected: Sensitive values and unsafe paths are absent or redacted.

73. Open Continuity. Export continuity state, inspect bundle/manifest/references/validation/report, run a dry-run import, then import into a clean test profile.
   - Expected: Dry run changes nothing. Import produces the expected resumable state and flags unresolved references.

74. Interrupt or cancel a running agent/coding/command workflow, restart Neo, then open Recovery.
   - Expected: Recovery finds the incomplete run. Resume, retry, fork, and repair actions require confirmation and preserve an audit trail.

75. Open GitHub. Configure a test-only connection, run health check, import a test issue and pull request, create a task, inspect operations, and request a PR draft if enabled.
   - Expected: Imported content is correct, the token is never displayed, and external writes require explicit approval.

76. Use an invalid GitHub token, invalid item number, duplicate import, and unreachable service.
   - Expected: Neo displays a clear error/degraded state and does not create corrupt duplicate records.

## 13. CLI, TUI, and final resilience checks

77. Run `neo health`, `neo status`, and representative commands from `research`, `providers`, `eval`, `workspace`, `continuity`, `coding`, `agentic`, `recovery`, `rules`, `tools`, `tests`, `git`, and `bundles`.
   - Expected: Each command connects to the API, gives a readable result, and reports failures without an unhelpful traceback.

78. Repeat a representative command with `--json` or `NEO_CLI_OUTPUT=json`.
   - Expected: Standard output is valid JSON with no extra prose.

79. Use CLI `--yes` for an approval-sensitive workflow.
   - Expected: It only skips a local CLI prompt. Neo’s backend approval requirement still prevents unapproved execution.

80. Start `neo tui` and navigate its main views.
   - Expected: The TUI opens, responds to navigation, and shows meaningful unavailable/API-error states.

81. In the browser, test keyboard navigation, Escape to close dialogs, browser Back/Forward, a narrow window, refresh during loading, and temporary network loss.
   - Expected: Focus remains visible, dialogs close cleanly, navigation is stable, and failures have a recovery message instead of a broken screen.

82. Restart the production-shaped container or backend without deleting its data volume.
   - Expected: Health succeeds and the test profile, records, managed-copy state, and audit history remain available.

## Final decision

Approve the release only when every applicable step passes and these statements are true:

- No action changed an original repository.
- No profile accessed another profile’s data.
- No secret/raw credential appeared in the interface, logs, exports, or reports.
- All unavailable providers and integrations showed an honest unavailable/error state.
- All approval-controlled actions remained blocked until approved.
- Restart, recovery, persistence, and health checks passed.

If any expected result does not occur, record the step number, exact observed behavior, screenshot/output, and build version. Do not release until P0 or P1 problems are fixed and retested.
