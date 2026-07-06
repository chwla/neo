import { useEffect, useRef, useState } from "react";

import { api } from "./api.js";

export default function FileAttachments({ linkType, targetId, onOpenFile }) {
  const [files, setFiles] = useState([]);
  const [allFiles, setAllFiles] = useState([]);
  const [attachId, setAttachId] = useState("");
  const [error, setError] = useState("");
  const input = useRef(null);

  async function load() {
    const params = { [`${linkType}Id`]: targetId };
    const [linked, all] = await Promise.all([api.filesList(params), api.filesList()]);
    setFiles(linked.files || []);
    setAllFiles(all.files || []);
  }

  useEffect(() => { if (targetId) load().catch((err) => setError(err.message)); }, [linkType, targetId]);
  const attachable = allFiles.filter((item) => !files.some((linked) => linked.id === item.id));

  async function upload(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    setError("");
    try {
      const links = { [`${linkType}Id`]: targetId };
      await api.uploadFile(file, links);
      await load();
    } catch (err) { setError(err.message); }
    event.target.value = "";
  }

  async function attach() {
    if (!attachId) return;
    try { await api.attachFile(attachId, linkType, targetId); setAttachId(""); await load(); }
    catch (err) { setError(err.message); }
  }

  return (
    <section className="file-attachments">
      <div className="file-attachments-title">Files</div>
      <div className="file-attachments-actions">
        <input ref={input} type="file" hidden onChange={upload} />
        <button type="button" onClick={() => input.current?.click()}>Upload file</button>
        <select value={attachId} onChange={(event) => setAttachId(event.target.value)}>
          <option value="">Attach existing…</option>
          {attachable.map((item) => <option key={item.id} value={item.id}>{item.metadata?.relative_path || item.display_name}</option>)}
        </select>
        <button type="button" disabled={!attachId} onClick={attach}>Attach</button>
      </div>
      {files.length ? <div className="file-attachment-list">{files.map((item) => (
        <button type="button" key={item.id} onClick={() => onOpenFile?.(item.id)}>
          <strong>{item.metadata?.relative_path || item.display_name}</strong><span>{item.extension || "file"} · {item.size_bytes} bytes</span>
        </button>
      ))}</div> : <p className="task-help">No files attached.</p>}
      {error ? <div className="task-error">{error}</div> : null}
    </section>
  );
}
