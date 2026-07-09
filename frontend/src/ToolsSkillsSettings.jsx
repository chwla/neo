import { useEffect, useState } from "react";

import { api } from "./api.js";

const SERVER_EMPTY = {
  name: "",
  server_type: "stdio",
  command_json_text: "[\"python\", \"--version\"]",
  url: "",
  env_json_text: "{}",
  enabled: true,
  approval_required: true,
  metadata_text: "{}",
};

const TOOL_EMPTY = {
  server_id: "",
  name: "",
  display_name: "",
  description: "",
  category: "external_read",
  input_schema_text: "{}",
  output_schema_text: "{}",
  permissions_text: "{}",
  enabled: true,
  metadata_text: "{}",
};

const SKILL_EMPTY = {
  name: "",
  display_name: "",
  description: "",
  skill_type: "instruction_bundle",
  instructions: "",
  tool_ids: [],
  agent_ids_text: "",
  rules_profile_ids_text: "",
  enabled: true,
  metadata_text: "{}",
};

function label(value) {
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function parseJson(text, fallback) {
  if (!String(text || "").trim()) return fallback;
  return JSON.parse(text);
}

function lines(text) {
  return String(text || "").split("\n").map((item) => item.trim()).filter(Boolean);
}

export default function ToolsSkillsSettings({ onClose }) {
  const [servers, setServers] = useState([]);
  const [tools, setTools] = useState([]);
  const [skills, setSkills] = useState([]);
  const [calls, setCalls] = useState([]);
  const [serverForm, setServerForm] = useState(SERVER_EMPTY);
  const [toolForm, setToolForm] = useState(TOOL_EMPTY);
  const [skillForm, setSkillForm] = useState(SKILL_EMPTY);
  const [editingServer, setEditingServer] = useState(null);
  const [editingTool, setEditingTool] = useState(null);
  const [editingSkill, setEditingSkill] = useState(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  async function load() {
    const [serverData, toolData, skillData, callData] = await Promise.all([
      api.toolServers(true),
      api.toolDefinitions(true),
      api.toolSkills(true),
      api.toolCalls({ limit: 50 }),
    ]);
    setServers(serverData.servers || []);
    setTools(toolData.definitions || []);
    setSkills(skillData.skills || []);
    setCalls(callData.calls || []);
  }

  useEffect(() => { load().catch((error) => setMessage(error.message)); }, []);

  function fromServer(server) {
    return {
      ...SERVER_EMPTY,
      ...server,
      command_json_text: JSON.stringify(server.command_json || [], null, 2),
      env_json_text: JSON.stringify(server.env_json || {}, null, 2),
      metadata_text: JSON.stringify(server.metadata || {}, null, 2),
    };
  }

  function fromTool(tool) {
    return {
      ...TOOL_EMPTY,
      ...tool,
      server_id: tool.server_id || "",
      input_schema_text: JSON.stringify(tool.input_schema || {}, null, 2),
      output_schema_text: JSON.stringify(tool.output_schema || {}, null, 2),
      permissions_text: JSON.stringify(tool.permissions || {}, null, 2),
      metadata_text: JSON.stringify(tool.metadata || {}, null, 2),
    };
  }

  function fromSkill(skill) {
    return {
      ...SKILL_EMPTY,
      ...skill,
      agent_ids_text: (skill.agent_ids || []).join("\n"),
      rules_profile_ids_text: (skill.rules_profile_ids || []).join("\n"),
      metadata_text: JSON.stringify(skill.metadata || {}, null, 2),
    };
  }

  async function saveServer(event) {
    event.preventDefault();
    setBusy(true); setMessage("");
    try {
      const payload = {
        name: serverForm.name,
        server_type: serverForm.server_type,
        command_json: serverForm.server_type === "stdio" ? parseJson(serverForm.command_json_text, []) : null,
        url: serverForm.url || null,
        env_json: parseJson(serverForm.env_json_text, {}),
        enabled: serverForm.enabled,
        approval_required: serverForm.approval_required,
        metadata: parseJson(serverForm.metadata_text, {}),
      };
      if (editingServer) await api.updateToolServer(editingServer.id, payload);
      else await api.createToolServer(payload);
      setEditingServer(null); setServerForm(SERVER_EMPTY); await load(); setMessage("Tool server saved.");
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function saveTool(event) {
    event.preventDefault();
    setBusy(true); setMessage("");
    try {
      const payload = {
        server_id: toolForm.server_id || null,
        name: toolForm.name,
        display_name: toolForm.display_name || null,
        description: toolForm.description || null,
        category: toolForm.category,
        input_schema: parseJson(toolForm.input_schema_text, {}),
        output_schema: parseJson(toolForm.output_schema_text, {}),
        permissions: parseJson(toolForm.permissions_text, {}),
        enabled: toolForm.enabled,
        metadata: parseJson(toolForm.metadata_text, {}),
      };
      if (editingTool) await api.updateToolDefinition(editingTool.id, payload);
      else await api.createToolDefinition(payload);
      setEditingTool(null); setToolForm(TOOL_EMPTY); await load(); setMessage("Tool definition saved.");
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function saveSkill(event) {
    event.preventDefault();
    setBusy(true); setMessage("");
    try {
      const payload = {
        name: skillForm.name,
        display_name: skillForm.display_name || null,
        description: skillForm.description || null,
        skill_type: skillForm.skill_type,
        instructions: skillForm.instructions,
        tool_ids: skillForm.tool_ids || [],
        agent_ids: lines(skillForm.agent_ids_text),
        rules_profile_ids: lines(skillForm.rules_profile_ids_text),
        enabled: skillForm.enabled,
        metadata: parseJson(skillForm.metadata_text, {}),
      };
      if (editingSkill) await api.updateToolSkill(editingSkill.id, payload);
      else await api.createToolSkill(payload);
      setEditingSkill(null); setSkillForm(SKILL_EMPTY); await load(); setMessage("Skill saved.");
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  async function action(fn, success) {
    setBusy(true); setMessage("");
    try {
      const result = await fn();
      await load();
      setMessage(success(result));
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <div className="settings-dialog tools-settings-dialog" role="dialog" aria-modal="true" aria-label="Tools and Skills">
        <div className="modal-header"><h2>Tools &amp; Skills</h2><button type="button" onClick={onClose}>×</button></div>
        <p className="dialog-caption">Tools cannot bypass approvals. Secrets must use environment variable references. Dangerous tools are disabled.</p>
        <div className="agent-settings-grid tools-settings-grid">
          <section className="settings-section">
            <h3>Tool servers</h3>
            <div className="agent-definition-list">
              {servers.map((server) => <button type="button" key={server.id} className={!server.enabled ? "disabled" : ""} onClick={() => { setEditingServer(server); setServerForm(fromServer(server)); }}>
                <strong>{server.name}</strong><span>{server.server_type} · {server.enabled ? "enabled" : "disabled"} · approval {server.approval_required ? "required" : "optional"}</span>
              </button>)}
            </div>
            <div className="settings-actions">{editingServer && <button type="button" onClick={() => action(() => api.toolServerHealth(editingServer.id), (result) => `Health: ${result.health.status}`)} disabled={busy}>Health</button>}{editingServer && <button type="button" onClick={() => action(() => api.discoverToolServer(editingServer.id), (result) => `Discovered ${result.definitions.length} tool(s).`)} disabled={busy}>Discover</button>}{editingServer && <button type="button" onClick={() => action(() => api.disableToolServer(editingServer.id), () => "Server disabled.")} disabled={busy || !editingServer.enabled}>Disable</button>}</div>
            <form onSubmit={saveServer} className="agent-definition-form">
              <h4>{editingServer ? "Edit server" : "Create server"}</h4>
              <label>Name<input value={serverForm.name} onChange={(event) => setServerForm({ ...serverForm, name: event.target.value })} required /></label>
              <label>Type<select value={serverForm.server_type} onChange={(event) => setServerForm({ ...serverForm, server_type: event.target.value })} disabled={Boolean(editingServer)}><option value="stdio">stdio MCP</option><option value="http">HTTP MCP</option></select></label>
              <label>Command argv JSON<textarea rows={2} value={serverForm.command_json_text} onChange={(event) => setServerForm({ ...serverForm, command_json_text: event.target.value })} /></label>
              <label>URL<input value={serverForm.url || ""} onChange={(event) => setServerForm({ ...serverForm, url: event.target.value })} /></label>
              <label>Env refs JSON<textarea rows={2} value={serverForm.env_json_text} onChange={(event) => setServerForm({ ...serverForm, env_json_text: event.target.value })} /></label>
              <label>Metadata JSON<textarea rows={3} value={serverForm.metadata_text} onChange={(event) => setServerForm({ ...serverForm, metadata_text: event.target.value })} /></label>
              <label><input type="checkbox" checked={serverForm.enabled} onChange={(event) => setServerForm({ ...serverForm, enabled: event.target.checked })} /> Enabled</label>
              <label><input type="checkbox" checked={serverForm.approval_required} onChange={(event) => setServerForm({ ...serverForm, approval_required: event.target.checked })} /> Approval required by default</label>
              <div className="settings-actions"><button type="submit" disabled={busy}>{editingServer ? "Save server" : "Create server"}</button><button type="button" onClick={() => { setEditingServer(null); setServerForm(SERVER_EMPTY); }}>New</button></div>
            </form>
          </section>
          <section className="settings-section">
            <h3>Tool definitions</h3>
            <div className="agent-definition-list">
              {tools.map((tool) => <button type="button" key={tool.id} className={!tool.enabled ? "disabled" : ""} onClick={() => { setEditingTool(tool); setToolForm(fromTool(tool)); }}>
                <strong>{tool.display_name || tool.name}</strong><span>{label(tool.category)} · {tool.enabled ? "enabled" : "disabled"}{tool.built_in ? " · built-in" : ""}</span>
              </button>)}
            </div>
            {editingTool && <div className="settings-actions"><button type="button" onClick={() => action(() => api.disableToolDefinition(editingTool.id), () => "Tool disabled.")} disabled={busy || !editingTool.enabled}>Disable</button></div>}
            <form onSubmit={saveTool} className="agent-definition-form">
              <h4>{editingTool ? "Edit tool" : "Create tool"}</h4>
              <label>Name<input value={toolForm.name} onChange={(event) => setToolForm({ ...toolForm, name: event.target.value })} disabled={Boolean(editingTool)} required /></label>
              <label>Server<select value={toolForm.server_id || ""} onChange={(event) => setToolForm({ ...toolForm, server_id: event.target.value })}><option value="">No server</option>{servers.map((server) => <option key={server.id} value={server.id}>{server.name}</option>)}</select></label>
              <label>Display name<input value={toolForm.display_name || ""} onChange={(event) => setToolForm({ ...toolForm, display_name: event.target.value })} /></label>
              <label>Description<textarea rows={2} value={toolForm.description || ""} onChange={(event) => setToolForm({ ...toolForm, description: event.target.value })} /></label>
              <label>Category<select value={toolForm.category} onChange={(event) => setToolForm({ ...toolForm, category: event.target.value })}>{["read_only", "workspace_read", "workspace_write_approval_required", "external_read", "external_write_approval_required", "dangerous_disabled"].map((item) => <option key={item} value={item}>{label(item)}</option>)}</select></label>
              <label>Input schema JSON<textarea rows={3} value={toolForm.input_schema_text} onChange={(event) => setToolForm({ ...toolForm, input_schema_text: event.target.value })} /></label>
              <label>Permissions JSON<textarea rows={2} value={toolForm.permissions_text} onChange={(event) => setToolForm({ ...toolForm, permissions_text: event.target.value })} /></label>
              <label>Metadata JSON<textarea rows={2} value={toolForm.metadata_text} onChange={(event) => setToolForm({ ...toolForm, metadata_text: event.target.value })} /></label>
              <label><input type="checkbox" checked={toolForm.enabled} onChange={(event) => setToolForm({ ...toolForm, enabled: event.target.checked })} /> Enabled</label>
              <div className="settings-actions"><button type="submit" disabled={busy}>{editingTool ? "Save tool" : "Create tool"}</button><button type="button" onClick={() => { setEditingTool(null); setToolForm(TOOL_EMPTY); }}>New</button></div>
            </form>
          </section>
          <section className="settings-section">
            <h3>Skills</h3>
            <div className="agent-definition-list">
              {skills.map((skill) => <button type="button" key={skill.id} className={!skill.enabled ? "disabled" : ""} onClick={() => { setEditingSkill(skill); setSkillForm(fromSkill(skill)); }}>
                <strong>{skill.display_name || skill.name}</strong><span>{skill.tool_ids.length} tool(s) · {skill.enabled ? "enabled" : "disabled"}{skill.built_in ? " · built-in" : ""}</span>
              </button>)}
            </div>
            {editingSkill && <div className="settings-actions"><button type="button" onClick={() => action(() => api.disableToolSkill(editingSkill.id), () => "Skill disabled.")} disabled={busy || !editingSkill.enabled}>Disable</button></div>}
            <form onSubmit={saveSkill} className="agent-definition-form">
              <h4>{editingSkill ? "Edit skill" : "Create skill"}</h4>
              <label>Name<input value={skillForm.name} onChange={(event) => setSkillForm({ ...skillForm, name: event.target.value })} disabled={Boolean(editingSkill)} required /></label>
              <label>Display name<input value={skillForm.display_name || ""} onChange={(event) => setSkillForm({ ...skillForm, display_name: event.target.value })} /></label>
              <label>Description<textarea rows={2} value={skillForm.description || ""} onChange={(event) => setSkillForm({ ...skillForm, description: event.target.value })} /></label>
              <label>Instructions<textarea rows={5} value={skillForm.instructions} onChange={(event) => setSkillForm({ ...skillForm, instructions: event.target.value })} required /></label>
              <fieldset className="agent-permissions"><legend>Allowed tools</legend>{tools.map((tool) => <label key={tool.id}><input type="checkbox" checked={(skillForm.tool_ids || []).includes(tool.id)} onChange={(event) => setSkillForm({ ...skillForm, tool_ids: event.target.checked ? [...(skillForm.tool_ids || []), tool.id] : (skillForm.tool_ids || []).filter((id) => id !== tool.id) })} />{tool.display_name || tool.name}</label>)}</fieldset>
              <label>Preferred agents<textarea rows={2} value={skillForm.agent_ids_text} onChange={(event) => setSkillForm({ ...skillForm, agent_ids_text: event.target.value })} /></label>
              <label>Rules profiles<textarea rows={2} value={skillForm.rules_profile_ids_text} onChange={(event) => setSkillForm({ ...skillForm, rules_profile_ids_text: event.target.value })} /></label>
              <label>Metadata JSON<textarea rows={2} value={skillForm.metadata_text} onChange={(event) => setSkillForm({ ...skillForm, metadata_text: event.target.value })} /></label>
              <label><input type="checkbox" checked={skillForm.enabled} onChange={(event) => setSkillForm({ ...skillForm, enabled: event.target.checked })} /> Enabled</label>
              <div className="settings-actions"><button type="submit" disabled={busy}>{editingSkill ? "Save skill" : "Create skill"}</button><button type="button" onClick={() => { setEditingSkill(null); setSkillForm(SKILL_EMPTY); }}>New</button></div>
            </form>
          </section>
        </div>
        <section className="settings-section">
          <h3>Call history &amp; approvals</h3>
          {calls.length ? calls.map((call) => <div className="llm-route-row" key={call.id}><strong>{call.tool_id}</strong><span>{call.status} · approval {call.approval_status} · {call.latency_ms ?? "—"} ms{call.error ? ` · ${call.error}` : ""}</span>{call.approval_status === "pending" && <button type="button" onClick={() => action(() => api.approveToolCall(call.id), () => "Tool call approved.")} disabled={busy}>Approve</button>}{call.approval_status === "pending" && <button type="button" onClick={() => action(() => api.rejectToolCall(call.id, "Rejected in settings."), () => "Tool call rejected.")} disabled={busy}>Reject</button>}</div>) : <p className="dialog-caption">No tool calls recorded yet.</p>}
        </section>
        {message ? <div className={message.toLowerCase().includes("error") ? "task-error" : "settings-status"}>{message}</div> : null}
      </div>
    </div>
  );
}
