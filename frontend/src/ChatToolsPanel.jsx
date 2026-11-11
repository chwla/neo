import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";

/**
 * Neo's tool preferences for one chat. Each toggle covers every backend tool in that
 * category (see TOOL_GROUP_ROWS below and the `group` field the backend
 * annotates each tool with) -- turning one off disables every tool in it for
 * this chat alone. A few tools (todo_write, create_checkpoint,
 * deliver_changes) are ungrouped and always stay on; they never appear here.
 */
const TOOL_GROUP_ROWS = [
  {
    key: "shell",
    label: "Shell",
    description: "Run inspection commands and tests in the attached folder.",
  },
  {
    key: "file_operations",
    label: "File operations",
    description: "Work with files and create folders in the attached workspace.",
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

export default function ChatToolsPanel({ chatId, onClose, onOpenFolder, mode, executor }) {
  const [tools, setTools] = useState([]);
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState(null);
  const [busyKey, setBusyKey] = useState(null);
  const needsFolder = tools.some((tool) => tool.unavailable_reason === "folder_required");

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
        <p>Choose which tools Neo may use in this chat.</p>
        {executor && executor !== "neo" ? (
          <p>The selected external engine manages its own tools.</p>
        ) : mode === "plan" ? (
          <p>In Plan mode, tools can propose changes for review.</p>
        ) : null}
      </div>
      {!loading && needsFolder ? (
        <div className="chat-tools-toolbar">
          <p>Attach a folder to use local file and command tools.</p>
          <button type="button" className="secondary-button" onClick={onOpenFolder}>
            Open a folder
          </button>
        </div>
      ) : null}

      {loading ? (
        <p className="open-folder-empty">Loading tools…</p>
      ) : !tools.length && notice ? (
        <button type="button" className="secondary-button" onClick={() => {
          setNotice(null);
          load();
        }}>Retry</button>
      ) : (
        <ul className="chat-tools-list">
          {TOOL_GROUP_ROWS.map((row) => {
            const { members, enabled } = groupState(tools, row.key);
            const available = members.filter((tool) => tool.available).length;
            const status = !members.some((tool) => tool.enabled)
              ? "Off for this chat"
              : available === members.length
                ? "Available"
                : available
                  ? `${available} of ${members.length} tools available`
                  : "Unavailable";
            return (
              <li key={row.key} className="chat-tools-row">
                <div className="chat-tools-row-info">
                  <div className="chat-tools-row-title">
                    <strong>{row.label}</strong>
                  </div>
                  <p>{row.description}</p>
                  <p id={`chat-tool-status-${row.key}`}>{status}</p>
                </div>
                <label className="chat-tools-toggle">
                  <input
                    type="checkbox"
                    checked={enabled}
                    disabled={busyKey !== null || !members.length}
                    onChange={() => toggleGroup(row.key)}
                    aria-label={`${enabled ? "Disable" : "Enable"} ${row.label} for this chat`}
                    aria-describedby={`chat-tool-status-${row.key}`}
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
