import { useEffect, useState } from "react";

import { api } from "./api.js";

const EMPTY = {
  name: "",
  description: "",
  scope_type: "workspace",
  scope_id: "",
  priority: 100,
  enabled: true,
  rules: { instructions: [] },
};

export default function RulesProfiles({ onClose }) {
  const [profiles, setProfiles] = useState([]);
  const [logs, setLogs] = useState([]);
  const [form, setForm] = useState(EMPTY);
  const [rulesJson, setRulesJson] = useState(JSON.stringify(EMPTY.rules, null, 2));
  const [editingId, setEditingId] = useState(null);
  const [preview, setPreview] = useState(null);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  async function load() {
    try {
      const [profileData, logData] = await Promise.all([api.ruleProfiles(), api.ruleLogs()]);
      setProfiles(profileData.profiles || []);
      setLogs(logData.resolution_logs || []);
    } catch (loadError) {
      setError(loadError.message);
    }
  }

  useEffect(() => { load(); }, []);

  function reset() {
    setEditingId(null);
    setForm(EMPTY);
    setRulesJson(JSON.stringify(EMPTY.rules, null, 2));
    setError("");
  }

  function edit(profile) {
    setEditingId(profile.id);
    setForm({
      name: profile.name,
      description: profile.description || "",
      scope_type: profile.scope_type,
      scope_id: profile.scope_id || "",
      priority: profile.priority,
      enabled: profile.enabled,
      rules: profile.rules,
    });
    setRulesJson(JSON.stringify(profile.rules, null, 2));
    setMessage("");
    setError("");
  }

  async function save(event) {
    event.preventDefault();
    setError("");
    try {
      const rules = JSON.parse(rulesJson);
      if (!rules || Array.isArray(rules) || typeof rules !== "object") {
        throw new Error("Rules JSON must be an object.");
      }
      const payload = {
        ...form,
        name: form.name.trim(),
        description: form.description.trim() || null,
        scope_id: form.scope_id.trim() || null,
        priority: Number(form.priority),
        rules,
      };
      if (!payload.name) throw new Error("Profile name is required.");
      if (!['workspace', 'global'].includes(payload.scope_type) && !payload.scope_id) {
        throw new Error("Scope ID is required for this scope.");
      }
      if (editingId) await api.updateRuleProfile(editingId, payload);
      else await api.createRuleProfile(payload);
      setMessage(editingId ? "Profile changes saved." : "Profile created.");
      reset();
      await load();
    } catch (saveError) {
      setError(saveError instanceof SyntaxError ? `Invalid rules JSON: ${saveError.message}` : saveError.message);
    }
  }

  async function toggle(profile) {
    setError("");
    try {
      await api.updateRuleProfile(profile.id, { enabled: !profile.enabled });
      setMessage(profile.enabled ? "Profile disabled." : "Profile re-enabled.");
      await load();
    } catch (toggleError) {
      setError(toggleError.message);
    }
  }

  async function resolve() {
    setError("");
    try {
      setPreview(await api.resolveRules({
        context_type: "coding_agent",
        project_id: form.scope_type === "project" ? form.scope_id || null : null,
        repo_id: form.scope_type === "repo" ? form.scope_id || null : null,
        task_id: form.scope_type === "task" ? form.scope_id || null : null,
        coding_run_id: form.scope_type === "coding_run" ? form.scope_id || null : null,
      }));
      await load();
    } catch (resolveError) {
      setError(resolveError.message);
    }
  }

  async function importRepo(repoId) {
    setError("");
    try {
      const result = await api.importRepoRules(repoId);
      setMessage(`Imported ${result.profiles.length} rule file(s).`);
      if (result.warnings?.length) setError(result.warnings.join(" "));
      await load();
    } catch (importError) {
      setError(importError.message);
    }
  }

  return <div className="modal-backdrop"><section className="neo-dialog neo-dialog-wide" role="dialog" aria-modal="true" aria-label="Rules & Profiles">
    <div className="dialog-title-row"><h2>Rules &amp; Profiles</h2><button className="dialog-close" aria-label="Close rules" onClick={onClose}>×</button></div>
    <p className="dialog-caption">Scoped guidance for Neo. Rules never grant permission or override safety.</p>
    <form className="coding-agent-form" onSubmit={save}>
      <h3>{editingId ? "Edit profile" : "Create profile"}</h3>
      <label>Name<input aria-label="Profile name" value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
      <label>Description<input aria-label="Profile description" value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
      <div className="coding-agent-selectors">
        <label>Scope<select aria-label="Profile scope" value={form.scope_type} onChange={(event) => setForm({ ...form, scope_type: event.target.value })}><option value="workspace">Workspace</option><option value="project">Project</option><option value="repo">Repo</option><option value="task">Task</option><option value="coding_run">Coding run</option></select></label>
        <label>Scope ID<input aria-label="Profile scope ID" value={form.scope_id} onChange={(event) => setForm({ ...form, scope_id: event.target.value })} placeholder="Required except workspace" /></label>
        <label>Priority<input aria-label="Profile priority" type="number" value={form.priority} onChange={(event) => setForm({ ...form, priority: event.target.value })} /></label>
        <label>Enabled<input aria-label="Profile enabled" type="checkbox" checked={form.enabled} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} /></label>
      </div>
      <label>Rules JSON<textarea aria-label="Rules JSON" rows="10" value={rulesJson} onChange={(event) => setRulesJson(event.target.value)} /></label>
      <div className="coding-agent-buttons"><button type="submit">{editingId ? "Save changes" : "Create profile"}</button>{editingId && <button type="button" onClick={reset}>Cancel edit</button>}<button type="button" onClick={resolve}>Resolve preview</button>{form.scope_type === "repo" && form.scope_id && <button type="button" onClick={() => importRepo(form.scope_id)}>Import from repo</button>}</div>
    </form>
    {error && <div className="neo-error">{error}</div>}
    {message && <div className="settings-status">{message}</div>}
    {preview && <section><h3>Resolve preview</h3>{preview.warnings?.map((warning, index) => <div className="neo-error" key={index}>{warning}</div>)}<pre>{JSON.stringify(preview.resolved_rules, null, 2)}</pre></section>}
    <section><h3>Profiles</h3>{profiles.map((profile) => <div className="llm-route-row" key={profile.id}><strong>{profile.name}</strong><span>{profile.scope_type}{profile.scope_id ? ` · ${profile.scope_id}` : ""} · priority {profile.priority} · {profile.enabled ? "enabled" : "disabled"}</span><button type="button" onClick={() => edit(profile)}>Edit</button><button type="button" onClick={() => toggle(profile)}>{profile.enabled ? "Disable" : "Re-enable"}</button>{profile.scope_type === "repo" && <button type="button" onClick={() => importRepo(profile.scope_id)}>Import repo</button>}</div>)}</section>
    <section><h3>Resolution logs</h3>{logs.slice(0, 20).map((log) => <div key={log.id}><strong>{log.context_type}</strong> · {log.applied_profiles.length} profiles · {log.warnings.length} warnings</div>)}</section>
  </section></div>;
}
