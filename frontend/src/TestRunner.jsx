import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";

const WARNING = "This command will run inside Neo’s managed workspace copy only.\n\nIt will not run in the original repository. It cannot use shell chaining, Git, package install commands, or destructive commands.";

function argvText(command) { return JSON.stringify(command || []); }
function duration(value) { return value == null ? "—" : `${value} ms`; }

export default function TestRunner({ repo, compact = false }) {
  const [commands, setCommands] = useState([]);
  const [suggestions, setSuggestions] = useState([]);
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState(null);
  const [form, setForm] = useState({ name: "", command: '["python", "-m", "pytest", "-q"]', working_directory: ".", timeout_seconds: 120 });
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const load = useCallback(async () => {
    if (!repo?.id) return;
    const [commandData, runData] = await Promise.all([api.testCommands(repo.id), api.testRuns({ repoId: repo.id })]);
    setCommands(commandData.commands || []); setRuns(runData.runs || []);
  }, [repo?.id]);
  useEffect(() => { load().catch((error) => setMessage(error.message)); }, [load]);

  async function detect() {
    setBusy(true); setMessage("");
    try { const data = await api.detectTestCommands(repo.id); setSuggestions(data.suggestions || []); }
    catch (error) { setMessage(error.message); } finally { setBusy(false); }
  }
  async function save(event) {
    event.preventDefault(); setBusy(true); setMessage("");
    try {
      const command = JSON.parse(form.command);
      await api.createTestCommand(repo.id, { ...form, command, timeout_seconds: Number(form.timeout_seconds) });
      setForm({ ...form, name: "" }); await load();
    } catch (error) { setMessage(error.message); } finally { setBusy(false); }
  }
  async function saveSuggestion(item) {
    setBusy(true); setMessage("");
    try { await api.createTestCommand(repo.id, item); await load(); }
    catch (error) { setMessage(error.message); } finally { setBusy(false); }
  }
  async function run(item) {
    if (!window.confirm(`${WARNING}\n\nRun: ${item.command.join(" ")}?`)) return;
    setBusy(true); setMessage("Running in the managed copy…");
    try { const data = await api.runTestCommand(item.id, { confirm: true }); setSelected(data.run); await load(); setMessage(`Test run ${data.run.status}.`); }
    catch (error) { setMessage(error.message); } finally { setBusy(false); }
  }
  async function open(runId) {
    try { const data = await api.testRun(runId); setSelected(data.run); }
    catch (error) { setMessage(error.message); }
  }
  async function checkpointFromRun() {
    if (!selected || !window.confirm("Create a local Git checkpoint for the current managed-workspace changes and link this test result? The original repository is never modified.")) return;
    setBusy(true); setMessage("");
    try {
      await api.createGitCheckpoint(repo.id, {
        title: `After ${selected.name}`,
        message: `Test result: ${selected.status}, exit ${selected.exit_code ?? "none"}.`,
        test_run_id: selected.id,
        patch_application_id: selected.patch_application_id || null,
      });
      setMessage("Checkpoint created from this test result.");
    } catch (error) { setMessage(error.message); } finally { setBusy(false); }
  }

  return <section className={`test-runner ${compact ? "compact" : ""}`}>
    <div className="test-runner-title"><div><h3>Test Runner</h3><p>Explicit, allowlisted tests in Neo’s managed repository copy.</p></div><button type="button" disabled={busy} onClick={detect}>Detect commands</button></div>
    {suggestions.length > 0 && <div className="test-suggestions"><strong>Suggestions (never run automatically)</strong>{suggestions.map((item) => <div key={argvText(item.command)}><span>{item.name}<code>{argvText(item.command)}</code></span><button type="button" disabled={busy} onClick={() => saveSuggestion(item)}>Save</button></div>)}</div>}
    {!compact && <form className="test-command-form" onSubmit={save}>
      <input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} placeholder="Command name" required />
      <input value={form.command} onChange={(event) => setForm({ ...form, command: event.target.value })} placeholder='["python", "-m", "pytest", "-q"]' required />
      <input value={form.working_directory} onChange={(event) => setForm({ ...form, working_directory: event.target.value })} placeholder="Working directory" required />
      <input type="number" min="1" max="600" value={form.timeout_seconds} onChange={(event) => setForm({ ...form, timeout_seconds: event.target.value })} />
      <button type="submit" disabled={busy}>Save command</button>
    </form>}
    <div className="test-runner-grid"><div><h4>Saved commands</h4>{commands.length ? commands.map((item) => <div className="test-command" key={item.id}><span><strong>{item.name}</strong><code>{argvText(item.command)}</code><small>{item.working_directory} · {item.timeout_seconds}s {item.enabled ? "" : "· disabled"}</small></span><button type="button" disabled={busy || !item.enabled} onClick={() => run(item)}>Run</button></div>) : <p>No saved commands.</p>}</div>
      <div><h4>Latest runs</h4>{runs.length ? runs.map((item) => <button className="test-run-row" type="button" key={item.id} onClick={() => open(item.id)}><strong>{item.name}</strong><span className={`test-status ${item.status}`}>{item.status}</span><small>exit {item.exit_code ?? "—"} · {duration(item.duration_ms)}</small></button>) : <p>No test runs yet.</p>}</div></div>
    {selected && <div className="test-run-detail"><div><strong>Test run details</strong><button type="button" onClick={() => setSelected(null)}>Close</button></div><p><span className={`test-status ${selected.status}`}>{selected.status}</span> · exit {selected.exit_code ?? "—"} · {duration(selected.duration_ms)}<br /><code>{argvText(selected.command)}</code><br />cwd: {selected.working_directory}</p><label>stdout<pre>{selected.stdout_text || "(empty)"}</pre></label><label>stderr<pre>{selected.stderr_text || "(empty)"}</pre></label>{selected.error && <div className="task-error">{selected.error}</div>}<button type="button" disabled={busy} onClick={checkpointFromRun}>Create checkpoint from this test result</button></div>}
    {message && <div className={message.includes("Running") || message.includes("run ") || message.includes("created") ? "repos-message" : "task-error"}>{message}</div>}
  </section>;
}
