import { useEffect, useMemo, useState } from "react";

import { api } from "./api.js";

const EMPTY = {
  name: "",
  display_name: "",
  description: "",
  agent_type: "custom",
  system_prompt: "",
  default_route_name: "",
  rules_profile_ids: [],
  permissions: {
    can_plan: true,
    can_read_files: true,
    can_select_context: true,
    can_propose_patch: false,
    can_request_patch_apply: false,
    can_request_tests: false,
    can_request_checkpoint: false,
    can_research: false,
    can_delegate: false,
    max_delegations: 0,
    allowed_file_patterns: [],
    forbidden_file_patterns: [],
  },
  tools: [],
  skills: [],
  enabled: true,
  priority: 100,
  metadata: {},
};

const PERMISSIONS = [
  "can_plan",
  "can_read_files",
  "can_select_context",
  "can_propose_patch",
  "can_request_patch_apply",
  "can_request_tests",
  "can_request_checkpoint",
  "can_research",
  "can_delegate",
];

function label(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function cleanPayload(form) {
  return {
    ...form,
    default_route_name: form.default_route_name || null,
    rules_profile_ids: form.rules_profile_ids || [],
    permissions: {
      ...form.permissions,
      max_delegations: Number(form.permissions.max_delegations || 0),
      allowed_file_patterns: String(form.permissions.allowed_file_patterns_text || "")
        .split("\n").map((item) => item.trim()).filter(Boolean),
      forbidden_file_patterns: String(form.permissions.forbidden_file_patterns_text || "")
        .split("\n").map((item) => item.trim()).filter(Boolean),
    },
  };
}

function fromAgent(agent) {
  return {
    ...EMPTY,
    ...agent,
    default_route_name: agent.default_route_name || "",
    permissions: {
      ...EMPTY.permissions,
      ...(agent.permissions || {}),
      allowed_file_patterns_text: (agent.permissions?.allowed_file_patterns || []).join("\n"),
      forbidden_file_patterns_text: (agent.permissions?.forbidden_file_patterns || []).join("\n"),
    },
  };
}

export default function AgentSettings({ onClose }) {
  const [agents, setAgents] = useState([]);
  const [profiles, setProfiles] = useState([]);
  const [routes, setRoutes] = useState([]);
  const [tools, setTools] = useState([]);
  const [skills, setSkills] = useState([]);
  const [editing, setEditing] = useState(null);
  const [form, setForm] = useState(EMPTY);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  const customAgents = useMemo(() => agents.filter((agent) => !agent.built_in), [agents]);

  async function load() {
    const [agentData, profileData, routeData, toolData, skillData] = await Promise.all([
      api.agentDefinitions(true),
      api.ruleProfiles(),
      api.llmRoutes(),
      api.toolDefinitions(true),
      api.toolSkills(true),
    ]);
    setAgents(agentData.definitions || []);
    setProfiles(profileData.profiles || []);
    setRoutes(routeData.routes || []);
    setTools(toolData.definitions || []);
    setSkills(skillData.skills || []);
  }

  useEffect(() => { load().catch((error) => setMessage(error.message)); }, []);

  function edit(agent) {
    setEditing(agent);
    setForm(fromAgent(agent));
  }

  function reset() {
    setEditing(null);
    setForm(EMPTY);
  }

  async function save(event) {
    event.preventDefault();
    setBusy(true); setMessage("");
    try {
      const payload = cleanPayload(form);
      if (editing) {
        await api.updateAgentDefinition(editing.id, payload);
      } else {
        await api.createAgentDefinition(payload);
      }
      await load();
      reset();
      setMessage("Agent saved. Unsafe permissions, if any, were clamped.");
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function disable(agent) {
    setBusy(true); setMessage("");
    try {
      await api.disableAgentDefinition(agent.id);
      await load();
      setMessage(`${agent.display_name || agent.name} disabled. Disabled agents cannot run.`);
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function resetBuiltins() {
    setBusy(true); setMessage("");
    try {
      await api.resetBuiltinAgents();
      await load();
      setMessage("Built-in agents reset idempotently.");
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <div className="settings-dialog agent-settings-dialog" role="dialog" aria-modal="true" aria-label="Agents">
        <div className="modal-header"><h2>Agents</h2><button type="button" onClick={onClose}>×</button></div>
        <p className="dialog-caption">Agents cannot bypass approvals. Unsafe permissions are clamped; disabled agents cannot run.</p>
        <div className="agent-settings-grid">
          <section className="settings-section">
            <div className="settings-actions"><button type="button" onClick={resetBuiltins} disabled={busy}>Reset built-ins</button><button type="button" onClick={reset}>New custom agent</button></div>
            <h3>Built-ins</h3>
            <div className="agent-definition-list">
              {agents.filter((agent) => agent.built_in).map((agent) => (
                <button type="button" key={agent.id} onClick={() => edit(agent)} className={!agent.enabled ? "disabled" : ""}>
                  <strong>{agent.display_name || agent.name}</strong>
                  <span>{agent.description}</span>
                  <small>{agent.enabled ? "Enabled" : "Disabled"} · {agent.agent_type}</small>
                </button>
              ))}
            </div>
            <h3>Custom</h3>
            <div className="agent-definition-list">
              {customAgents.length ? customAgents.map((agent) => (
                <button type="button" key={agent.id} onClick={() => edit(agent)} className={!agent.enabled ? "disabled" : ""}>
                  <strong>{agent.display_name || agent.name}</strong>
                  <span>{agent.description || "No description."}</span>
                  <small>{agent.enabled ? "Enabled" : "Disabled"} · {agent.agent_type}</small>
                </button>
              )) : <p>No custom agents yet.</p>}
            </div>
          </section>
          <form className="settings-section agent-definition-form" onSubmit={save}>
            <h3>{editing ? `Edit ${editing.display_name || editing.name}` : "Create custom agent"}</h3>
            {editing?.built_in ? <p className="task-help">Built-ins allow enable/disable, route, profile, and metadata edits only. Reset restores defaults.</p> : null}
            <label>Name<input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} disabled={Boolean(editing)} required /></label>
            <label>Display name<input value={form.display_name || ""} onChange={(event) => setForm({ ...form, display_name: event.target.value })} /></label>
            <label>Description<textarea rows={2} value={form.description || ""} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
            <label>Type<select value={form.agent_type} onChange={(event) => setForm({ ...form, agent_type: event.target.value })} disabled={Boolean(editing)}>
              {["custom", "general", "planner", "coder", "reviewer", "tester", "researcher", "refactor", "explorer", "summarizer"].map((type) => <option key={type} value={type}>{label(type)}</option>)}
            </select></label>
            <label>Model route<select value={form.default_route_name || ""} onChange={(event) => setForm({ ...form, default_route_name: event.target.value })}>
              <option value="">Use rules/default route</option>
              {routes.map((route) => <option key={route.id} value={route.route_name}>{route.route_name}</option>)}
            </select></label>
            <label>Rule profiles<select multiple value={form.rules_profile_ids || []} onChange={(event) => setForm({ ...form, rules_profile_ids: Array.from(event.target.selectedOptions).map((option) => option.value) })}>
              {profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
            </select></label>
            <label>System prompt<textarea rows={5} value={form.system_prompt} onChange={(event) => setForm({ ...form, system_prompt: event.target.value })} disabled={editing?.built_in} required /></label>
            <fieldset className="agent-permissions"><legend>Permissions</legend>
              {PERMISSIONS.map((key) => <label key={key}><input type="checkbox" checked={Boolean(form.permissions[key])} onChange={(event) => setForm({ ...form, permissions: { ...form.permissions, [key]: event.target.checked } })} disabled={editing?.built_in} />{label(key)}</label>)}
              <label>Max delegations<input type="number" min="0" max="5" value={form.permissions.max_delegations || 0} onChange={(event) => setForm({ ...form, permissions: { ...form.permissions, max_delegations: event.target.value } })} disabled={editing?.built_in} /></label>
              <label>Allowed file patterns<textarea rows={2} value={form.permissions.allowed_file_patterns_text || ""} onChange={(event) => setForm({ ...form, permissions: { ...form.permissions, allowed_file_patterns_text: event.target.value } })} disabled={editing?.built_in} /></label>
              <label>Forbidden file patterns<textarea rows={2} value={form.permissions.forbidden_file_patterns_text || ""} onChange={(event) => setForm({ ...form, permissions: { ...form.permissions, forbidden_file_patterns_text: event.target.value } })} disabled={editing?.built_in} /></label>
            </fieldset>
            <fieldset className="agent-permissions"><legend>Allowed tools</legend>
              <p className="task-help">Empty means read-only built-ins only. Mutating tools still require approval.</p>
              {tools.map((tool) => <label key={tool.id}><input type="checkbox" checked={(form.tools || []).includes(tool.id)} onChange={(event) => setForm({ ...form, tools: event.target.checked ? [...(form.tools || []), tool.id] : (form.tools || []).filter((id) => id !== tool.id) })} />{tool.display_name || tool.name} · {label(tool.category)}{!tool.enabled ? " (disabled)" : ""}</label>)}
            </fieldset>
            <fieldset className="agent-permissions"><legend>Allowed skills</legend>
              {skills.map((skill) => <label key={skill.id}><input type="checkbox" checked={(form.skills || []).includes(skill.id)} onChange={(event) => setForm({ ...form, skills: event.target.checked ? [...(form.skills || []), skill.id] : (form.skills || []).filter((id) => id !== skill.id) })} />{skill.display_name || skill.name}{!skill.enabled ? " (disabled)" : ""}</label>)}
            </fieldset>
            <label><input type="checkbox" checked={Boolean(form.enabled)} onChange={(event) => setForm({ ...form, enabled: event.target.checked })} /> Enabled</label>
            {editing?.safety_warnings?.length ? <div className="task-error">{editing.safety_warnings.join(" ")}</div> : null}
            <div className="settings-actions"><button type="submit" disabled={busy}>{editing ? "Save changes" : "Create agent"}</button>{editing && <button type="button" onClick={() => disable(editing)} disabled={busy || !editing.enabled}>Disable</button>}</div>
          </form>
        </div>
        {message ? <div className={message.toLowerCase().includes("error") ? "task-error" : "settings-status"}>{message}</div> : null}
      </div>
    </div>
  );
}
