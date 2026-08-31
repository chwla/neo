import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";

/**
 * The agent's tools for one chat, collapsed into 4 toggles: Shell, File
 * operations, Search, Memory. Each toggle covers every backend tool in that
 * category (see TOOL_GROUP_ROWS below and the `group` field the backend
 * annotates each tool with) -- turning one off disables every tool in it for
 * this chat alone. A few tools (todo_write, create_checkpoint,
 * deliver_changes) are ungrouped and always stay on; they never appear here.
 */
const TOOL_GROUP_ROWS = [
  {
    key: "shell",
    label: "Shell",
    description: "Run shell commands and tests in the attached repository.",
  },
  {
    key: "file_operations",
    label: "File operations",
    description: "Read, write, edit, and delete files in the attached repository.",
  },
  {
    key: "search",
    label: "Search",
    description: "Search the repository and the web for information.",
  },
  {
    key: "memory",
    label: "Memory",
    description: "Recall earlier context saved from past chats.",
  },
  {
    key: "calendar",
    label: "Calendar",
    description: "Read the calendar, and propose adding, changing, or removing events (always asks first).",
  },
  {
    key: "gallery",
    label: "Gallery",
    description: "Find and read images you have shown Neo before, by what was in them.",
  },
];

function groupState(tools, groupKey) {
  const members = tools.filter((tool) => tool.group === groupKey);
  return { members, enabled: members.length > 0 && members.every((tool) => tool.enabled) };
}

export default function ChatToolsPanel({ chatId, onClose }) {
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState(null);
  const [busyKey, setBusyKey] = useState(null);

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

  async function toggleGroup(groupKey) {
    setBusyKey(groupKey);
    setNotice(null);
    const { members, enabled } = groupState(tools, groupKey);
    const memberNames = new Set(members.map((tool) => tool.name));
    const stillDisabled = tools
      .filter((tool) => !tool.enabled && !memberNames.has(tool.name))
      .map((tool) => tool.name);
    const nextDisabled = enabled ? [...stillDisabled, ...memberNames] : stillDisabled;
    try {
      await api.updateChat(chatId, { disabled_tools: nextDisabled });
      await load();
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    } finally {
      setBusyKey(null);
    }
  }

  return (
    <Modal title="Tools" onClose={onClose} wide className="chat-tools-panel">
      {notice ? <div className={`connector-notice ${notice.type}`}>{notice.text}</div> : null}

      <div className="chat-tools-toolbar">
        <p>Tools the agent can use in this chat. Turning one off here does not disable it elsewhere.</p>
      </div>

      {loading ? (
        <p className="open-folder-empty">Loading tools…</p>
      ) : (
        <ul className="chat-tools-list">
          {TOOL_GROUP_ROWS.map((row) => {
            const { enabled } = groupState(tools, row.key);
            return (
              <li key={row.key} className="chat-tools-row">
                <div className="chat-tools-row-info">
                  <div className="chat-tools-row-title">
                    <strong>{row.label}</strong>
                  </div>
                  <p>{row.description}</p>
                </div>
                <label className="chat-tools-toggle">
                  <input
                    type="checkbox"
                    checked={enabled}
                    disabled={busyKey === row.key}
                    onChange={() => toggleGroup(row.key)}
                    aria-label={`${enabled ? "Disable" : "Enable"} ${row.label} for this chat`}
                  />
                </label>
              </li>
            );
          })}
        </ul>
      )}
    </Modal>
  );
}
