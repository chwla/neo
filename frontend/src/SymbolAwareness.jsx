import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";

export default function SymbolAwareness({ repo, repoFiles, indexReady, onOpenFile, compact }) {
  const [status, setStatus] = useState(null);
  const [tab, setTab] = useState("definitions");
  const [name, setName] = useState("");
  const [repoFileId, setRepoFileId] = useState("");
  const [items, setItems] = useState([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const loadStatus = useCallback(async () => {
    if (!repo?.id) return;
    const data = await api.symbolAwareness(repo.id);
    setStatus(data);
  }, [repo?.id]);

  useEffect(() => { setItems([]); loadStatus().catch((error) => setMessage(error.message)); }, [loadStatus]);
  useEffect(() => { if (!repoFileId && repoFiles?.length) setRepoFileId(repoFiles[0].id); }, [repoFileId, repoFiles]);

  async function build() {
    setBusy(true); setMessage("");
    try {
      const data = await api.buildSymbolAwareness(repo.id, ["ready", "partial"].includes(status?.status));
      setStatus(data); setMessage("Symbol Awareness built from static managed-workspace content.");
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }

  async function search() {
    setBusy(true); setMessage("");
    try {
      if (tab === "definitions") setItems((await api.symbolDefinitions(repo.id, name.trim())).definitions || []);
      else if (tab === "references") setItems((await api.symbolReferencesByName(repo.id, name.trim())).references || []);
      else if (tab === "document") setItems((await api.documentSymbols(repo.id, repoFileId)).symbols || []);
      else setItems((await api.relatedCodeFiles(repo.id, repoFileId)).related_files || []);
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }

  function open(item) {
    onOpenFile?.(item.file_id || item.source_file_id || item.target_file_id);
  }

  const stats = status?.stats || {};
  const ready = ["ready", "partial"].includes(status?.status);
  return <section className={`symbol-awareness-panel ${compact ? "compact" : ""}`}>
    <div className="code-index-header"><div><h3>Symbol Awareness</h3><p>Status: {status?.status || "not built"}</p></div><button type="button" disabled={busy || !indexReady} onClick={build}>{ready ? "Rebuild Symbol Awareness" : "Build Symbol Awareness"}</button></div>
    {!indexReady && <p className="repos-message">Build Codebase Index first.</p>}
    {ready && <div className="code-index-stats"><span>{stats.reference_count || 0} references</span><span>{stats.resolved_reference_count || 0} resolved</span><span>{stats.relationship_count || 0} relationships</span><span>{stats.related_file_count || 0} related files</span></div>}
    {message && <p className="repos-message">{message}</p>}
    {ready && !compact && <>
      <div className="code-index-tabs">{["definitions", "references", "document", "related"].map((item) => <button type="button" className={tab === item ? "selected" : ""} key={item} onClick={() => { setTab(item); setItems([]); }}>{item}</button>)}</div>
      {(tab === "definitions" || tab === "references") ? <input value={name} onChange={(event) => setName(event.target.value)} placeholder={tab === "definitions" ? "Find symbol definition" : "Find symbol references"} /> : <select value={repoFileId} onChange={(event) => setRepoFileId(event.target.value)}>{(repoFiles || []).map((file) => <option key={file.id} value={file.id}>{file.relative_path}</option>)}</select>}
      <button type="button" disabled={busy || ((tab === "definitions" || tab === "references") ? !name.trim() : !repoFileId)} onClick={search}>Search</button>
      <div className="code-index-results">{items.map((item, index) => <button type="button" key={item.id || item.symbol_id || `${item.source_relative_path}-${item.line_start}-${index}`} onClick={() => open(item)}>
        <strong>{item.name || item.referenced_name || item.target_relative_path}</strong>
        <span>{item.relative_path || item.source_relative_path || item.target_relative_path}{item.line_start ? `:${item.line_start}` : ""}</span>
        <small>{item.symbol_type || item.reference_type || item.relationship_type}{item.confidence ? ` · ${Math.round(item.confidence * 100)}%` : ""}</small>
      </button>)}</div>
    </>}
  </section>;
}
