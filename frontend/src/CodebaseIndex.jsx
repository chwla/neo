import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";

export default function CodebaseIndex({ repo, onOpenFile, compact = false }) {
  const [index, setIndex] = useState(null);
  const [tab, setTab] = useState("symbols");
  const [query, setQuery] = useState("");
  const [items, setItems] = useState([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const loadIndex = useCallback(async () => {
    if (!repo?.id) return;
    try { const data = await api.codeIndex(repo.id); setIndex(data.index); }
    catch (error) { if (!String(error.message).includes("not been built")) setMessage(error.message); else setIndex(null); }
  }, [repo?.id]);

  const loadTab = useCallback(async () => {
    if (!repo?.id || !index) return;
    if (tab === "routes") setItems((await api.codeRoutes(repo.id)).routes || []);
    else if (tab === "dependencies") setItems((await api.codeDependencies(repo.id)).dependencies || []);
    else if (tab === "search") setItems(query.trim() ? (await api.codeSearch(repo.id, query.trim())).results || [] : []);
    else setItems((await api.codeSymbols(repo.id, { q: query.trim() })).symbols || []);
  }, [index, query, repo?.id, tab]);

  useEffect(() => { setIndex(null); setItems([]); loadIndex(); }, [loadIndex]);
  useEffect(() => { loadTab().catch((error) => setMessage(error.message)); }, [loadTab]);

  async function build() {
    setBusy(true); setMessage("");
    try {
      const data = await api.buildCodeIndex(repo.id, Boolean(index));
      setIndex(data.index); setMessage("Static codebase index built from the managed workspace copy.");
    } catch (error) { setMessage(error.message); }
    finally { setBusy(false); }
  }

  function open(item) {
    if (item.file_id) onOpenFile?.(item.file_id);
  }

  return <section className={`code-index-panel ${compact ? "compact" : ""}`}>
    <div className="code-index-header"><div><h3>Codebase Index</h3><p>Status: {index?.status || "not built"}</p></div><button type="button" disabled={busy} onClick={build}>{index ? "Rebuild Index" : "Build Index"}</button></div>
    {index && <div className="code-index-stats"><span>{index.indexed_file_count} files</span><span>{index.symbol_count} symbols</span><span>{index.route_count} routes</span><span>{index.dependency_count} dependencies</span><span>{index.indexed_at ? new Date(index.indexed_at).toLocaleString() : "pending"}</span></div>}
    {message && <p className="repos-message">{message}</p>}
    {index && !compact && <>
      <div className="code-index-tabs">{["symbols", "routes", "dependencies", "search"].map((name) => <button type="button" className={tab === name ? "selected" : ""} key={name} onClick={() => { setTab(name); setItems([]); }}>{name}</button>)}</div>
      {(tab === "symbols" || tab === "search") && <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={tab === "search" ? "Search codebase purpose, paths, symbols" : "Search symbols"} />}
      <div className="code-index-results">{items.map((item, indexValue) => <button type="button" key={item.id || `${item.relative_path}-${item.name || item.import_text}-${indexValue}`} onClick={() => open(item)}>
        <strong>{item.name || item.import_text || `${item.method} ${item.path}`}</strong>
        <span>{item.relative_path || item.source_relative_path}{item.line_start ? `:${item.line_start}` : ""}</span>
        <small>{item.symbol_type || item.dependency_type || item.handler || item.summary}</small>
      </button>)}</div>
    </>}
  </section>;
}
