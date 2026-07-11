import { useEffect, useState } from "react";

import { api } from "./api.js";

const redactDisplay = (value) => JSON.stringify(value, null, 2)
  .replace(/file:\/\/\/[^\s"}]+|(?:\/[^\s,"}]+){2,}/g, "[REDACTED_PATH]");

export default function LspPanel() {
  const [workspaceId, setWorkspaceId] = useState("");
  const [status, setStatus] = useState(null);
  const [diagnostics, setDiagnostics] = useState(null);
  const [filePath, setFilePath] = useState("src/app.py");
  const [query, setQuery] = useState("");
  const [result, setResult] = useState(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const refresh = async () => {
    setBusy(true);
    try { setStatus(await api.lspStatus()); setMessage(""); }
    catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  };
  useEffect(() => { refresh(); }, []);
  const run = async (action) => {
    if (!workspaceId.trim()) { setMessage("Enter a managed repository ID."); return; }
    setBusy(true);
    try {
      const payload = action === "workspace-symbols"
        ? { query } : { file_path: filePath, line: 0, character: 0, language: "python" };
      const value = await api.lspQuery(workspaceId.trim(), action, payload);
      setResult(value); setMessage(value.status === "unavailable" ? value.reason : "Read-only LSP result received.");
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  };
  const session = async (kind) => {
    if (!workspaceId.trim()) { setMessage("Enter a managed repository ID."); return; }
    setBusy(true);
    try {
      const value = kind === "start" ? await api.lspStart(workspaceId.trim()) : await api.lspStop(workspaceId.trim());
      setResult(value); await refresh(); setMessage(value.reason || `LSP ${kind} completed.`);
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  };
  const loadDiagnostics = async () => {
    if (!workspaceId.trim()) { setMessage("Enter a managed repository ID."); return; }
    setBusy(true);
    try { setDiagnostics(await api.lspDiagnostics(workspaceId.trim())); setMessage(""); }
    catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  };
  return <section className="lsp-panel">
    <h3>Language Server Protocol</h3>
    <p className="task-help">Read-only code intelligence. Rename is preview-only; no edits are applied.</p>
    <div className="coding-agent-selectors"><label>Managed repository ID<input value={workspaceId} onChange={(event) => setWorkspaceId(event.target.value)} placeholder="repository id" /></label><label>Relative file<input value={filePath} onChange={(event) => setFilePath(event.target.value)} /></label></div>
    <div className="coding-agent-buttons"><button type="button" onClick={refresh} disabled={busy}>Refresh servers</button><button type="button" onClick={() => session("start")} disabled={busy}>Start</button><button type="button" onClick={() => session("stop")} disabled={busy}>Stop</button><button type="button" onClick={loadDiagnostics} disabled={busy}>Diagnostics</button></div>
    <div><strong>Server availability</strong>{status?.servers?.length ? <ul>{status.servers.map((server) => <li key={server.language}>{server.language}: {server.available ? "available" : "command not found — static-symbol fallback active"}</li>)}</ul> : <p>Loading server status…</p>}</div>
    <div className="coding-agent-buttons"><button type="button" onClick={() => run("hover")} disabled={busy}>Hover</button><button type="button" onClick={() => run("definition")} disabled={busy}>Definition</button><button type="button" onClick={() => run("references")} disabled={busy}>References</button><button type="button" onClick={() => run("document-symbols")} disabled={busy}>Document symbols</button><button type="button" onClick={() => run("rename-preview")} disabled={busy}>Rename preview (no edit)</button></div>
    <label>Workspace symbols<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="UserService" /></label><button type="button" onClick={() => run("workspace-symbols")} disabled={busy}>Search symbols</button>
    {diagnostics ? <details open><summary><strong>Diagnostics</strong></summary><pre>{redactDisplay(diagnostics)}</pre></details> : null}
    {result ? <details open><summary><strong>Read-only result</strong></summary><pre>{redactDisplay(result)}</pre></details> : null}
    {message ? <p className={message.includes("unavailable") || message.includes("not found") ? "task-help" : "agent-message"}>{message}</p> : null}
  </section>;
}
