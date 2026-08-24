import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";
import { ConnectorWizard, submitConnectorWizard } from "./ConnectorWizard.jsx";
import { CONNECTOR_FORM_EMPTY } from "./connectorForms.js";

/**
 * The agent's tools for one chat: every built-in plus every enabled
 * connector tool, each toggleable on/off for this chat alone. "Add a tool"
 * reuses the same connector wizard Settings -> Tools & Skills uses -- a tool
 * created here is a global definition (visible in every chat, and in that
 * Settings page too), not scoped to this conversation. The per-chat toggle
 * is what lets a chat opt out of a tool it does not want.
 */
export default function ChatToolsPanel({ chatId, onClose }) {
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState(null);
  const [busyName, setBusyName] = useState(null);
  const [wizardOpen, setWizardOpen] = useState(false);
  const [kind, setKind] = useState("mcp_http");
  const [form, setForm] = useState({ ...CONNECTOR_FORM_EMPTY });
  const [file, setFile] = useState(null);
  const [wizardBusy, setWizardBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api.chatTools(chatId);
      setTools(result.tools || []);
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    } finally {
      setLoading(false);
    }
  }, [chatId]);

  useEffect(() => {
    load();
  }, [load]);

  async function toggle(tool) {
    setBusyName(tool.name);
    setNotice(null);
    const stillDisabled = tools.filter((t) => !t.enabled && t.name !== tool.name).map((t) => t.name);
    const nextDisabled = tool.enabled ? [...stillDisabled, tool.name] : stillDisabled;
    try {
      await api.updateChat(chatId, { disabled_tools: nextDisabled });
      await load();
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    } finally {
      setBusyName(null);
    }
  }

  async function submitWizard(event) {
    event.preventDefault();
    setWizardBusy(true);
    setNotice(null);
    try {
      const result = await submitConnectorWizard(kind, form, file);
      setWizardOpen(false);
      setForm({ ...CONNECTOR_FORM_EMPTY });
      setFile(null);
      setNotice({
        type: "success",
        text: `${result.server.name} connected. ${result.definitions?.length ?? 0} tool(s) are ready to use.`,
      });
      await load();
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    } finally {
      setWizardBusy(false);
    }
  }

  return (
    <Modal title="Tools" onClose={onClose} wide className="chat-tools-panel">
      {notice ? <div className={`connector-notice ${notice.type}`}>{notice.text}</div> : null}

      {wizardOpen ? (
        <ConnectorWizard
          busy={wizardBusy}
          form={form}
          kind={kind}
          file={file}
          onCancel={() => setWizardOpen(false)}
          onFile={setFile}
          onForm={setForm}
          onKind={setKind}
          onSubmit={submitWizard}
        />
      ) : (
        <>
          <div className="chat-tools-toolbar">
            <p>Tools the agent can use in this chat. Turning one off here does not disable it elsewhere.</p>
            <button
              type="button"
              className="connector-button primary"
              onClick={() => setWizardOpen(true)}
            >
              Add a tool
            </button>
          </div>

          {loading ? (
            <p className="open-folder-empty">Loading tools…</p>
          ) : (
            <ul className="chat-tools-list">
              {tools.map((tool) => (
                <li key={tool.name} className="chat-tools-row">
                  <div className="chat-tools-row-info">
                    <div className="chat-tools-row-title">
                      <strong>{tool.display_name || tool.name}</strong>
                      {tool.server_name ? (
                        <span className="open-folder-tag muted">{tool.server_name}</span>
                      ) : null}
                    </div>
                    {tool.description ? <p>{tool.description}</p> : null}
                    {tool.requires_repo ? (
                      <small className="chat-tools-hint">Needs a repository attached to this chat.</small>
                    ) : null}
                  </div>
                  <label className="chat-tools-toggle">
                    <input
                      type="checkbox"
                      checked={tool.enabled}
                      disabled={busyName === tool.name}
                      onChange={() => toggle(tool)}
                      aria-label={`${tool.enabled ? "Disable" : "Enable"} ${tool.display_name || tool.name} for this chat`}
                    />
                  </label>
                </li>
              ))}
              {!tools.length ? <li className="open-folder-empty">No tools are available yet.</li> : null}
            </ul>
          )}
        </>
      )}
    </Modal>
  );
}
