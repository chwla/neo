import { useEffect, useState } from "react";
import { api } from "./api.js";

export default function RelatedMemories({ scopeType, scopeId }) {
  const [items, setItems] = useState([]); const [detail, setDetail] = useState(null);
  useEffect(() => { if (scopeId) api.memoryScope(scopeType, scopeId).then((value) => setItems(value.items || [])).catch(() => setItems([])); }, [scopeType, scopeId]);
  if (!scopeId) return null;
  return <section className="settings-section related-memories"><h3>Related memories</h3>{items.length ? <ul>{items.slice(0, 8).map((item) => <li key={item.id}><button type="button" onClick={async () => setDetail(await api.memoryItem(item.id))}>{item.memory_type} · {item.title}</button><small>source: {item.source_type} · {item.content_text.slice(0, 150)}</small></li>)}</ul> : <p className="task-help">No indexed memories for this entity yet.</p>}{detail ? <details open><summary>Memory detail</summary><p>{detail.content_text}</p><small>source: {detail.source_type}:{detail.source_id || "none"}</small></details> : null}</section>;
}
