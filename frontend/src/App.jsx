import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { api } from "./api.js";

const EMPTY_SIDEBAR = { projects: [], chats: [] };
const MEMORY_TYPES = [
  "identity",
  "preference",
  "goal_related",
  "project_related",
  "knowledge",
  "relationship",
  "life_fact",
];
const MEMORY_TABS = [
  ["profile", "Profile"],
  ["preferences", "Preferences"],
  ["goals", "Goals"],
  ["projects", "Projects"],
  ["events", "Events"],
  ["memories", "Memories"],
];

function formatMemoryType(value) {
  return value
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function optionalText(value) {
  const cleaned = value.trim();
  return cleaned ? cleaned : null;
}

function errorMessage(error) {
  if (!error) {
    return "";
  }
  return error.message || String(error);
}

function parseQueryId(params, key) {
  const value = params.get(key);
  if (!value) {
    return null;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function findChatInSidebar(sidebar, chatId) {
  for (const chat of sidebar.chats) {
    if (chat.id === chatId) {
      return chat;
    }
  }
  for (const project of sidebar.projects) {
    for (const chat of project.chats) {
      if (chat.id === chatId) {
        return chat;
      }
    }
  }
  return null;
}

function findProjectInSidebar(sidebar, projectId) {
  return sidebar.projects.find((project) => project.id === projectId) ?? null;
}

function clearSidebarQueryActions() {
  if (!window.location.search) {
    return;
  }
  window.history.replaceState({}, "", window.location.pathname);
}

function FolderIcon() {
  return (
    <svg className="project-folder-icon" viewBox="0 0 24 24" aria-hidden="true">
      <path
        d="M3 8a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M3 8h18v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function NeoButton({ children, className = "", type = "button", ...props }) {
  return (
    <button type={type} className={`neo-button ${className}`.trim()} {...props}>
      {children}
    </button>
  );
}

function Modal({ title, children, onClose, wide = false, className = "" }) {
  return (
    <div className="modal-backdrop" role="presentation">
      <section
        className={`neo-dialog ${wide ? "neo-dialog-wide" : ""} ${className}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="dialog-title-row">
          <h2>{title}</h2>
          <button className="dialog-close" onClick={onClose} aria-label="Close" type="button">
            {"\u00d7"}
          </button>
        </div>
        {children}
      </section>
    </div>
  );
}

function Sidebar({
  sidebar,
  activeChatId,
  selectedProjectId,
  showNewProjectForm,
  onToggleProjectForm,
  onCreateProject,
  onNewChat,
  onOpenChat,
  onDeleteChat,
  onDeleteProject,
  onOpenSettings,
}) {
  const [projectName, setProjectName] = useState("");

  function submitProject(event) {
    event.preventDefault();
    const cleaned = projectName.trim();
    if (!cleaned) {
      return;
    }
    onCreateProject(cleaned);
    setProjectName("");
  }

  return (
    <aside className="neo-sidebar">
      <div className="sidebar-title">Neo</div>
      <NeoButton className="w-full justify-start" onClick={() => onNewChat(selectedProjectId)}>
        + New Chat
      </NeoButton>
      <NeoButton className="mt-2 w-full justify-start" onClick={onToggleProjectForm}>
        + New Project
      </NeoButton>

      {showNewProjectForm && (
        <form className="sidebar-form" onSubmit={submitProject}>
          <label>
            <span>Project name</span>
            <input
              value={projectName}
              onChange={(event) => setProjectName(event.target.value)}
              placeholder="Research, work, ideas..."
            />
          </label>
          <NeoButton type="submit" className="sidebar-form-submit">
            Create
          </NeoButton>
        </form>
      )}

      <div className="sidebar-section">Projects</div>
      {sidebar.projects.length === 0 ? (
        <p className="sidebar-caption">No projects yet.</p>
      ) : (
        sidebar.projects.map((project) => (
          <details
            className="project-folder"
            key={project.id}
            open={project.id === selectedProjectId}
          >
            <summary>
              <FolderIcon />
              <span className="project-folder-title">{project.name}</span>
              <button
                className="project-folder-delete"
                type="button"
                title="Delete project"
                aria-label="Delete project"
                onClick={(event) => {
                  event.preventDefault();
                  onDeleteProject(project);
                }}
              >
                X
              </button>
            </summary>
            <button
              className="project-folder-new-chat"
              type="button"
              onClick={() => onNewChat(project.id)}
            >
              + New Chat
            </button>
            {project.chats.map((chat) => (
              <button
                key={chat.id}
                className={`project-chat-link ${chat.id === activeChatId ? "active" : ""}`}
                type="button"
                onClick={() => onOpenChat(chat.id)}
              >
                {chat.title}
              </button>
            ))}
          </details>
        ))
      )}

      <div className="sidebar-section">Chats</div>
      {sidebar.chats.length === 0 ? (
        <p className="sidebar-caption">No chats yet.</p>
      ) : (
        sidebar.chats.map((chat) => (
          <div
            className={`chat-item ${chat.id === activeChatId ? "active" : ""}`}
            key={chat.id}
            data-chat-id={chat.id}
          >
            <button
              className="chat-item-title"
              type="button"
              onClick={() => onOpenChat(chat.id)}
            >
              {chat.title}
            </button>
            <button
              className="chat-item-delete"
              type="button"
              title="Delete chat"
              aria-label="Delete chat"
              onClick={() => onDeleteChat(chat)}
            >
              X
            </button>
          </div>
        ))
      )}

      <div className="sidebar-spacer" />
      <div className="sidebar-settings-bar">
        <div className="sidebar-settings-button">
          <NeoButton onClick={onOpenSettings} title="Settings" aria-label="Settings">
            {"\u2699"}
          </NeoButton>
        </div>
      </div>
    </aside>
  );
}

function formatTokens(message) {
  return Number.isFinite(message.total_tokens) ? `${message.total_tokens} tokens` : "Tokens n/a";
}

function formatDuration(durationMs) {
  if (!Number.isFinite(durationMs)) {
    return "Time n/a";
  }
  if (durationMs < 1000) {
    return `${durationMs} ms`;
  }
  const seconds = durationMs / 1000;
  return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
}

function formatElapsedDuration(durationMs) {
  if (!Number.isFinite(durationMs)) {
    return "0.0 s";
  }
  const seconds = durationMs / 1000;
  return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
}

function splitGeneratedText(rawContent) {
  const openTag = "<think>";
  const closeTag = "</think>";
  const lowerContent = rawContent.toLowerCase();
  const thinkingParts = [];
  const contentParts = [];
  let cursor = 0;

  while (cursor < rawContent.length) {
    const start = lowerContent.indexOf(openTag, cursor);
    if (start === -1) {
      contentParts.push(rawContent.slice(cursor));
      break;
    }
    contentParts.push(rawContent.slice(cursor, start));
    const thinkingStart = start + openTag.length;
    const end = lowerContent.indexOf(closeTag, thinkingStart);
    if (end === -1) {
      thinkingParts.push(rawContent.slice(thinkingStart));
      break;
    }
    thinkingParts.push(rawContent.slice(thinkingStart, end));
    cursor = end + closeTag.length;
  }

  return {
    content: contentParts.join("").trim(),
    thinking: thinkingParts.join("\n\n").trim(),
  };
}

function previousUserMessage(messages, message) {
  const index = messages.findIndex((item) => item.id === message.id);
  for (let cursor = index - 1; cursor >= 0; cursor -= 1) {
    if (messages[cursor]?.role === "user") {
      return messages[cursor];
    }
  }
  return null;
}

function ChatMessage({
  message,
  messages,
  editingMessageId,
  editingValue,
  onCancelEdit,
  onCopy,
  onEdit,
  onRerun,
  onSaveEdit,
  onSetEditingValue,
  onToggleThinking,
  thinkingOpen,
}) {
  const isUser = message.role === "user";
  const isEditing = isUser && editingMessageId === message.id;
  const previousUser = isUser ? null : previousUserMessage(messages, message);

  return (
    <article className={`neo-chat-message ${isUser ? "user" : "assistant"}`}>
      <div className="message-bubble">
        {isEditing ? (
          <form
            className="message-edit-form"
            onSubmit={(event) => {
              event.preventDefault();
              onSaveEdit(message);
            }}
          >
            <textarea
              value={editingValue}
              onChange={(event) => onSetEditingValue(event.target.value)}
              rows={3}
              autoFocus
            />
            <div className="message-actions">
              <button type="submit">Save</button>
              <button type="button" onClick={onCancelEdit}>
                Cancel
              </button>
            </div>
          </form>
        ) : (
          <>
            <div className="chat-content">{message.content}</div>
            {message.failed && (
              <div className="chat-message-status">Not sent. Edit and try again.</div>
            )}
            {!isUser && (
              <div className="message-meta">
                <span>{formatTokens(message)}</span>
                <span>{formatDuration(message.duration_ms)}</span>
              </div>
            )}
            <div className="message-actions">
              <button type="button" onClick={() => onCopy(message.content)}>
                Copy
              </button>
              {isUser ? (
                <button type="button" onClick={() => onEdit(message)}>
                  Edit
                </button>
              ) : (
                <>
                  <button
                    type="button"
                    disabled={!previousUser}
                    onClick={() => previousUser && onRerun(previousUser.content)}
                  >
                    Rerun
                  </button>
                  <button type="button" onClick={() => onToggleThinking(message.id)}>
                    {thinkingOpen ? "Hide thinking" : "View thinking"}
                  </button>
                </>
              )}
            </div>
            {!isUser && thinkingOpen && (
              <div className="thinking-panel">
                {message.thinking || "No thinking process was returned for this message."}
              </div>
            )}
          </>
        )}
      </div>
    </article>
  );
}

function PendingAssistantMessage({ generation, elapsedMs }) {
  const hasThinking = Boolean(generation?.thinking);
  const hasContent = Boolean(generation?.content);

  return (
    <article className="neo-chat-message assistant thinking">
      <div className="message-bubble pending-message-bubble">
        <div className="pending-message-header">
          <span>Neo is generating</span>
          <span className="pending-message-timer">{formatElapsedDuration(elapsedMs)}</span>
        </div>
        <div className="thinking-panel live-thinking-panel">
          {hasThinking ? generation.thinking : "Waiting for model thinking..."}
        </div>
        {hasContent && <div className="chat-content live-answer">{generation.content}</div>}
      </div>
    </article>
  );
}

function ChatComposer({ disabled, value, onChange, onSubmit }) {
  const textareaRef = useRef(null);

  const resizeComposer = useCallback(() => {
    const textarea = textareaRef.current;
    if (!textarea) {
      return;
    }

    const styles = window.getComputedStyle(textarea);
    const maxHeight = Number.parseFloat(styles.getPropertyValue("--composer-max-height"));
    const minHeight = Number.parseFloat(styles.getPropertyValue("--composer-min-height"));
    const viewportMax = Math.max(132, Math.floor(window.innerHeight * 0.34));
    const boundedMax = Math.min(Number.isFinite(maxHeight) ? maxHeight : 224, viewportMax);
    const boundedMin = Number.isFinite(minHeight) ? minHeight : 42;

    textarea.style.height = "auto";
    const nextHeight = Math.min(Math.max(textarea.scrollHeight, boundedMin), boundedMax);
    textarea.style.height = `${nextHeight}px`;
    textarea.style.overflowY = textarea.scrollHeight > nextHeight ? "auto" : "hidden";
  }, []);

  useLayoutEffect(() => {
    resizeComposer();
  }, [resizeComposer, value]);

  useEffect(() => {
    window.addEventListener("resize", resizeComposer);
    return () => window.removeEventListener("resize", resizeComposer);
  }, [resizeComposer]);

  return (
    <div className="chat-input-wrap">
      <div className="chat-input-shell">
        <form className="chat-input-form" onSubmit={onSubmit}>
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(event) => {
              onChange(event.target.value);
              requestAnimationFrame(resizeComposer);
            }}
            onInput={resizeComposer}
            placeholder="Message Neo"
            rows={1}
            disabled={disabled}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <NeoButton
            type="submit"
            className="send-button"
            disabled={disabled || !value.trim()}
            aria-label="Send message"
            title="Send message"
          >
            {"\u2191"}
          </NeoButton>
        </form>
      </div>
      <div className="chat-input-disclaimer">
        Neo is an AI and it can make mistakes. Please double-check responses.
      </div>
    </div>
  );
}

function SettingsDialog({ onOpenMemory, onClose }) {
  return (
    <Modal title="Settings" onClose={onClose} className="settings-dialog">
      <p className="dialog-caption">App controls</p>
      <NeoButton className="w-full" onClick={onOpenMemory}>
        Memory
      </NeoButton>
    </Modal>
  );
}

function ConfirmDeleteDialog({ pendingDelete, onCancel, onConfirm }) {
  if (!pendingDelete) {
    return null;
  }

  const isChat = pendingDelete.type === "chat";
  const title = isChat ? `Delete chat ${pendingDelete.label}?` : `Delete project ${pendingDelete.label}?`;
  const caption = isChat
    ? "This will permanently delete the chat and its messages."
    : `This will permanently delete the project and ${pendingDelete.chatCount} chat(s) inside it.`;

  return (
    <Modal title="Confirm deletion" onClose={onCancel}>
      <p className="delete-copy">
        <strong>{title}</strong>
      </p>
      <p className="dialog-caption">{caption}</p>
      <div className="dialog-actions confirm-actions">
        <NeoButton className="danger" onClick={onConfirm}>
          Confirm
        </NeoButton>
        <NeoButton onClick={onCancel}>Cancel</NeoButton>
      </div>
    </Modal>
  );
}

function Field({ label, children }) {
  return (
    <label className="memory-field">
      <span>{label}</span>
      {children}
    </label>
  );
}

function FormActions({ onDelete, saving }) {
  return (
    <div className="memory-actions">
      <NeoButton type="submit" disabled={saving}>
        Save
      </NeoButton>
      <NeoButton type="button" disabled={saving} onClick={onDelete}>
        Delete
      </NeoButton>
    </div>
  );
}

function MemoryCard({ summary, children }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <section className={`memory-card ${expanded ? "is-open" : ""}`}>
      <div className="memory-card-head">
        <p className="memory-card-summary">{summary}</p>
        <button
          className="memory-card-edit"
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Hide" : "Edit"}
        </button>
      </div>
      {expanded && <div className="memory-card-body">{children}</div>}
    </section>
  );
}

function ProfileEditor({ records, refresh, setError }) {
  if (!records.length) {
    return <p className="dialog-caption">No profile facts stored yet.</p>;
  }

  return records.map((record) => (
    <ProfileForm key={record.id} record={record} refresh={refresh} setError={setError} />
  ));
}

function ProfileForm({ record, refresh, setError }) {
  const [key, setKey] = useState(record.key);
  const [value, setValue] = useState(record.value);
  const [saving, setSaving] = useState(false);

  async function save(event) {
    event.preventDefault();
    if (!key.trim() || !value.trim()) {
      return;
    }
    setSaving(true);
    setError("");
    try {
      await api.updateProfile(record.id, { key: key.trim(), value: value.trim() });
      await refresh();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    setSaving(true);
    setError("");
    try {
      await api.deleteProfile(record.id);
      await refresh();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <MemoryCard summary={`the user's ${record.key} is ${record.value}.`}>
      <form onSubmit={save}>
        <Field label="Label">
          <input value={key} onChange={(event) => setKey(event.target.value)} />
        </Field>
        <Field label="Memory">
          <textarea value={value} onChange={(event) => setValue(event.target.value)} />
        </Field>
        <FormActions onDelete={remove} saving={saving} />
      </form>
    </MemoryCard>
  );
}

function PreferenceEditor({ records, refresh, setError }) {
  if (!records.length) {
    return <p className="dialog-caption">No preferences stored yet.</p>;
  }

  return records.map((record) => (
    <PreferenceForm key={record.id} record={record} refresh={refresh} setError={setError} />
  ));
}

function PreferenceForm({ record, refresh, setError }) {
  const [category, setCategory] = useState(record.category);
  const [value, setValue] = useState(record.value);
  const [importance, setImportance] = useState(record.importance);
  const [saving, setSaving] = useState(false);

  async function save(event) {
    event.preventDefault();
    if (!category.trim() || !value.trim()) {
      return;
    }
    setSaving(true);
    setError("");
    try {
      await api.updatePreference(record.id, {
        category: category.trim(),
        value: value.trim(),
        importance: Number(importance),
      });
      await refresh();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    setSaving(true);
    setError("");
    try {
      await api.deletePreference(record.id);
      await refresh();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <MemoryCard summary={`the user likes ${record.value}.`}>
      <form onSubmit={save}>
        <Field label="Category">
          <input value={category} onChange={(event) => setCategory(event.target.value)} />
        </Field>
        <Field label="Preference">
          <textarea value={value} onChange={(event) => setValue(event.target.value)} />
        </Field>
        <Field label="Importance">
          <input
            type="number"
            min="1"
            max="10"
            step="1"
            value={importance}
            onChange={(event) => setImportance(event.target.value)}
          />
        </Field>
        <FormActions onDelete={remove} saving={saving} />
      </form>
    </MemoryCard>
  );
}

function GoalEditor({ records, refresh, setError }) {
  if (!records.length) {
    return <p className="dialog-caption">No active goals stored yet.</p>;
  }

  return records.map((record) => (
    <GoalForm key={record.id} record={record} refresh={refresh} setError={setError} />
  ));
}

function GoalForm({ record, refresh, setError }) {
  const [goal, setGoal] = useState(record.goal);
  const [description, setDescription] = useState(record.description ?? "");
  const [priority, setPriority] = useState(record.priority);
  const [saving, setSaving] = useState(false);

  async function save(event) {
    event.preventDefault();
    if (!goal.trim()) {
      return;
    }
    setSaving(true);
    setError("");
    try {
      await api.updateGoal(record.id, {
        goal: goal.trim(),
        description: optionalText(description),
        priority: Number(priority),
      });
      await refresh();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    setSaving(true);
    setError("");
    try {
      await api.deleteGoal(record.id);
      await refresh();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <MemoryCard summary={`the user wants to ${record.goal}.`}>
      <form onSubmit={save}>
        <Field label="Goal">
          <textarea value={goal} onChange={(event) => setGoal(event.target.value)} />
        </Field>
        <Field label="Notes">
          <textarea value={description} onChange={(event) => setDescription(event.target.value)} />
        </Field>
        <Field label="Priority">
          <input
            type="number"
            min="1"
            max="10"
            step="1"
            value={priority}
            onChange={(event) => setPriority(event.target.value)}
          />
        </Field>
        <FormActions onDelete={remove} saving={saving} />
      </form>
    </MemoryCard>
  );
}

function ProjectMemoryEditor({ records, refresh, refreshSidebar, setError }) {
  if (!records.length) {
    return <p className="dialog-caption">No projects stored yet.</p>;
  }

  return records.map((record) => (
    <ProjectMemoryForm
      key={record.id}
      record={record}
      refresh={refresh}
      refreshSidebar={refreshSidebar}
      setError={setError}
    />
  ));
}

function ProjectMemoryForm({ record, refresh, refreshSidebar, setError }) {
  const [name, setName] = useState(record.name);
  const [description, setDescription] = useState(record.description ?? "");
  const [priority, setPriority] = useState(record.priority);
  const [saving, setSaving] = useState(false);

  async function save(event) {
    event.preventDefault();
    if (!name.trim()) {
      return;
    }
    setSaving(true);
    setError("");
    try {
      await api.updateProjectMemory(record.id, {
        name: name.trim(),
        description: optionalText(description),
        priority: Number(priority),
      });
      await refresh();
      await refreshSidebar();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    setSaving(true);
    setError("");
    try {
      await api.deleteProjectMemory(record.id);
      await refresh();
      await refreshSidebar();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <MemoryCard summary={`the user is working on ${record.name}.`}>
      <form onSubmit={save}>
        <Field label="Project">
          <input value={name} onChange={(event) => setName(event.target.value)} />
        </Field>
        <Field label="Notes">
          <textarea value={description} onChange={(event) => setDescription(event.target.value)} />
        </Field>
        <Field label="Priority">
          <input
            type="number"
            min="1"
            max="10"
            step="1"
            value={priority}
            onChange={(event) => setPriority(event.target.value)}
          />
        </Field>
        <FormActions onDelete={remove} saving={saving} />
      </form>
    </MemoryCard>
  );
}

function EventEditor({ records, refresh, setError }) {
  if (!records.length) {
    return <p className="dialog-caption">No events stored yet.</p>;
  }

  return records.map((record) => (
    <EventForm key={record.id} record={record} refresh={refresh} setError={setError} />
  ));
}

function EventForm({ record, refresh, setError }) {
  const [eventText, setEventText] = useState(record.event);
  const [description, setDescription] = useState(record.description ?? "");
  const [eventDate, setEventDate] = useState(record.event_date ?? "");
  const [importance, setImportance] = useState(record.importance);
  const [saving, setSaving] = useState(false);

  async function save(event) {
    event.preventDefault();
    if (!eventText.trim()) {
      return;
    }
    setSaving(true);
    setError("");
    try {
      await api.updateEvent(record.id, {
        event: eventText.trim(),
        description: optionalText(description),
        event_date: optionalText(eventDate),
        importance: Number(importance),
      });
      await refresh();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    setSaving(true);
    setError("");
    try {
      await api.deleteEvent(record.id);
      await refresh();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <MemoryCard summary={record.event}>
      <form onSubmit={save}>
        <Field label="Event">
          <textarea value={eventText} onChange={(event) => setEventText(event.target.value)} />
        </Field>
        <Field label="Notes">
          <textarea value={description} onChange={(event) => setDescription(event.target.value)} />
        </Field>
        <Field label="Date">
          <input
            value={eventDate}
            onChange={(event) => setEventDate(event.target.value)}
            placeholder="YYYY-MM-DD"
          />
        </Field>
        <Field label="Importance">
          <input
            type="number"
            min="1"
            max="10"
            step="1"
            value={importance}
            onChange={(event) => setImportance(event.target.value)}
          />
        </Field>
        <FormActions onDelete={remove} saving={saving} />
      </form>
    </MemoryCard>
  );
}

function GeneralMemoryEditor({ records, refresh, setError }) {
  if (!records.length) {
    return <p className="dialog-caption">No general memories stored yet.</p>;
  }

  return records.map((record) => (
    <GeneralMemoryForm key={record.id} record={record} refresh={refresh} setError={setError} />
  ));
}

function GeneralMemoryForm({ record, refresh, setError }) {
  const [memoryText, setMemoryText] = useState(record.memory_text);
  const [memoryType, setMemoryType] = useState(record.memory_type);
  const [importance, setImportance] = useState(record.importance);
  const [saving, setSaving] = useState(false);

  async function save(event) {
    event.preventDefault();
    if (!memoryText.trim()) {
      return;
    }
    setSaving(true);
    setError("");
    try {
      await api.updateMemory(record.id, {
        memory_text: memoryText.trim(),
        memory_type: memoryType,
        importance: Number(importance),
      });
      await refresh();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    setSaving(true);
    setError("");
    try {
      await api.deleteMemory(record.id);
      await refresh();
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <MemoryCard summary={record.memory_text}>
      <form onSubmit={save}>
        <Field label="Memory">
          <textarea value={memoryText} onChange={(event) => setMemoryText(event.target.value)} />
        </Field>
        <Field label="Type">
          <select value={memoryType} onChange={(event) => setMemoryType(event.target.value)}>
            {MEMORY_TYPES.map((type) => (
              <option key={type} value={type}>
                {formatMemoryType(type)}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Importance">
          <input
            type="number"
            min="1"
            max="10"
            step="1"
            value={importance}
            onChange={(event) => setImportance(event.target.value)}
          />
        </Field>
        <FormActions onDelete={remove} saving={saving} />
      </form>
    </MemoryCard>
  );
}

function MemoryDialog({ onClose, refreshSidebar }) {
  const [activeTab, setActiveTab] = useState("profile");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const nextData = await api.memory();
      setData(nextData);
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  let content = null;
  if (loading) {
    content = <p className="dialog-caption">Loading memory...</p>;
  } else if (data) {
    const editorProps = { refresh, setError };
    if (activeTab === "profile") {
      content = <ProfileEditor records={data.profile} {...editorProps} />;
    } else if (activeTab === "preferences") {
      content = <PreferenceEditor records={data.preferences} {...editorProps} />;
    } else if (activeTab === "goals") {
      content = <GoalEditor records={data.goals} {...editorProps} />;
    } else if (activeTab === "projects") {
      content = (
        <ProjectMemoryEditor
          records={data.projects}
          refreshSidebar={refreshSidebar}
          {...editorProps}
        />
      );
    } else if (activeTab === "events") {
      content = <EventEditor records={data.events} {...editorProps} />;
    } else {
      content = <GeneralMemoryEditor records={data.memories} {...editorProps} />;
    }
  }

  return (
    <Modal title="Memory" onClose={onClose} wide className="memory-dialog">
      <div className="memory-tabs" role="tablist" aria-label="Memory sections">
        {MEMORY_TABS.map(([key, label]) => (
          <button
            key={key}
            className={key === activeTab ? "active" : ""}
            type="button"
            role="tab"
            aria-selected={key === activeTab}
            onClick={() => setActiveTab(key)}
          >
            {label}
          </button>
        ))}
      </div>
      {error && <div className="neo-error">{error}</div>}
      <div className="memory-scroll">{content}</div>
    </Modal>
  );
}

export default function App() {
  const [sidebar, setSidebar] = useState(EMPTY_SIDEBAR);
  const [activeChat, setActiveChat] = useState(null);
  const [messages, setMessages] = useState([]);
  const [selectedProjectId, setSelectedProjectId] = useState(null);
  const [showNewProjectForm, setShowNewProjectForm] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showMemory, setShowMemory] = useState(false);
  const [pendingDelete, setPendingDelete] = useState(null);
  const [composerValue, setComposerValue] = useState("");
  const [editingMessageId, setEditingMessageId] = useState(null);
  const [editingValue, setEditingValue] = useState("");
  const [openThinkingMessageId, setOpenThinkingMessageId] = useState(null);
  const [sending, setSending] = useState(false);
  const [streamingAssistant, setStreamingAssistant] = useState(null);
  const [generationStartedAt, setGenerationStartedAt] = useState(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [statusError, setStatusError] = useState("");
  const bootstrapped = useRef(false);

  const refreshSidebar = useCallback(async () => {
    const nextSidebar = await api.sidebar();
    setSidebar(nextSidebar);
    return nextSidebar;
  }, []);

  const loadChat = useCallback(async (chatId) => {
    const thread = await api.getChat(chatId);
    setActiveChat(thread.chat);
    setMessages(thread.messages);
    setSelectedProjectId(thread.chat.project_id);
    localStorage.setItem("neo-active-chat-id", String(thread.chat.id));
    return thread;
  }, []);

  const createActiveChat = useCallback(
    async (projectId = null, options = {}) => {
      const chat = await api.createChat(projectId);
      setActiveChat(chat);
      if (options.resetMessages !== false) {
        setMessages([]);
      }
      setSelectedProjectId(chat.project_id);
      localStorage.setItem("neo-active-chat-id", String(chat.id));
      await refreshSidebar();
      return chat;
    },
    [refreshSidebar],
  );

  useEffect(() => {
    if (bootstrapped.current) {
      return;
    }
    bootstrapped.current = true;

    async function bootstrap() {
      setStatusError("");
      try {
        const nextSidebar = await refreshSidebar();
        const params = new URLSearchParams(window.location.search);
        const openChatId = parseQueryId(params, "open_chat");
        const deleteChatId = parseQueryId(params, "request_delete_chat");
        const deleteProjectId = parseQueryId(params, "request_delete_project");
        const newProjectChatId = parseQueryId(params, "new_project_chat");
        const selectedProjectIdFromQuery = parseQueryId(params, "select_project");

        if (selectedProjectIdFromQuery) {
          setSelectedProjectId(selectedProjectIdFromQuery);
        }

        if (deleteChatId) {
          const chat =
            findChatInSidebar(nextSidebar, deleteChatId) ??
            (await api.getChat(deleteChatId).then((thread) => thread.chat).catch(() => null));
          if (chat) {
            setPendingDelete({
              type: "chat",
              id: chat.id,
              label: chat.title,
            });
          }
        }

        if (deleteProjectId) {
          const project = findProjectInSidebar(nextSidebar, deleteProjectId);
          if (project) {
            setPendingDelete({
              type: "project",
              id: project.id,
              label: project.name,
              chatCount: project.chats.length,
            });
          }
        }

        if (newProjectChatId) {
          await createActiveChat(newProjectChatId);
          clearSidebarQueryActions();
          return;
        }

        if (openChatId) {
          try {
            await loadChat(openChatId);
          } finally {
            clearSidebarQueryActions();
          }
          return;
        }

        const storedChatId = Number(localStorage.getItem("neo-active-chat-id"));
        if (storedChatId) {
          try {
            await loadChat(storedChatId);
            clearSidebarQueryActions();
            return;
          } catch {
            localStorage.removeItem("neo-active-chat-id");
          }
        }
        await createActiveChat(selectedProjectIdFromQuery);
        clearSidebarQueryActions();
      } catch (error) {
        setStatusError(errorMessage(error));
      }
    }

    bootstrap();
  }, [createActiveChat, loadChat, refreshSidebar]);

  useEffect(() => {
    if (!generationStartedAt) {
      return undefined;
    }
    const updateElapsed = () => setElapsedMs(Date.now() - generationStartedAt);
    updateElapsed();
    const intervalId = window.setInterval(updateElapsed, 100);
    return () => window.clearInterval(intervalId);
  }, [generationStartedAt]);

  async function handleCreateProject(name) {
    setStatusError("");
    try {
      const project = await api.createProject(name);
      setSelectedProjectId(project.id);
      setShowNewProjectForm(false);
      await refreshSidebar();
    } catch (error) {
      setStatusError(errorMessage(error));
    }
  }

  async function handleNewChat(projectId = null) {
    setStatusError("");
    try {
      await createActiveChat(projectId);
    } catch (error) {
      setStatusError(errorMessage(error));
    }
  }

  async function handleOpenChat(chatId) {
    setStatusError("");
    try {
      await loadChat(chatId);
    } catch (error) {
      setStatusError(errorMessage(error));
    }
  }

  function handleDeleteChat(chat) {
    setPendingDelete({
      type: "chat",
      id: chat.id,
      label: chat.title,
    });
  }

  function handleDeleteProject(project) {
    setPendingDelete({
      type: "project",
      id: project.id,
      label: project.name,
      chatCount: project.chats.length,
    });
  }

  async function confirmDeletion() {
    if (!pendingDelete) {
      return;
    }
    setStatusError("");
    try {
      if (pendingDelete.type === "chat") {
        await api.deleteChat(pendingDelete.id);
        if (activeChat?.id === pendingDelete.id) {
          await createActiveChat(selectedProjectId);
        }
      } else {
        await api.deleteProject(pendingDelete.id);
        if (selectedProjectId === pendingDelete.id || activeChat?.project_id === pendingDelete.id) {
          await createActiveChat(null);
        }
        setSelectedProjectId(null);
      }
      setPendingDelete(null);
      await refreshSidebar();
    } catch (error) {
      setStatusError(errorMessage(error));
    }
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const textArea = document.createElement("textarea");
      textArea.value = text;
      document.body.appendChild(textArea);
      textArea.select();
      document.execCommand("copy");
      document.body.removeChild(textArea);
    }
  }

  function handleEditMessage(message) {
    setEditingMessageId(message.id);
    setEditingValue(message.content);
  }

  async function handleSaveEditedMessage(message) {
    const cleaned = editingValue.trim();
    if (!cleaned) {
      return;
    }
    if (typeof message.id !== "number") {
      setMessages((current) =>
        current.map((item) => (item.id === message.id ? { ...item, content: cleaned } : item)),
      );
      setComposerValue(cleaned);
      setEditingMessageId(null);
      setEditingValue("");
      return;
    }
    if (!activeChat?.id) {
      return;
    }
    setStatusError("");
    try {
      const updated = await api.updateChatMessage(activeChat.id, message.id, cleaned);
      setMessages((current) => current.map((item) => (item.id === updated.id ? updated : item)));
      setEditingMessageId(null);
      setEditingValue("");
      await refreshSidebar();
    } catch (error) {
      setStatusError(errorMessage(error));
    }
  }

  async function sendPrompt(prompt) {
    if (!prompt || sending) {
      return;
    }

    setSending(true);
    setStatusError("");
    setGenerationStartedAt(Date.now());
    setElapsedMs(0);
    setStreamingAssistant({
      rawContent: "",
      content: "",
      thinking: "",
    });
    const pendingId = `pending-${Date.now()}`;
    const optimisticMessage = {
      id: pendingId,
      chat_id: activeChat?.id ?? null,
      role: "user",
      content: prompt,
      created_at: new Date().toISOString(),
    };
    setMessages((current) => [...current, optimisticMessage]);

    try {
      const chat = activeChat ?? (await createActiveChat(selectedProjectId, { resetMessages: false }));
      let rawContent = "";
      await api.streamMessage(chat.id, prompt, (event) => {
        if (event.type === "chunk") {
          rawContent += event.content;
          setStreamingAssistant({
            rawContent,
            ...splitGeneratedText(rawContent),
          });
        }
      });
      await loadChat(chat.id);
      await refreshSidebar();
    } catch (error) {
      setMessages((current) =>
        current.map((message) =>
          message.id === pendingId ? { ...message, failed: true } : message,
        ),
      );
      setComposerValue(prompt);
      setStatusError(`${errorMessage(error)}. Your message was not sent, but it was kept.`);
    } finally {
      setSending(false);
      setGenerationStartedAt(null);
      setStreamingAssistant(null);
    }
  }

  async function handleSendMessage(event) {
    event.preventDefault();
    const prompt = composerValue.trim();
    if (!prompt || sending) {
      return;
    }
    setComposerValue("");
    await sendPrompt(prompt);
  }

  const showEmptyState = messages.length === 0 && !sending;

  return (
    <div className={`neo-app ${sidebarCollapsed ? "sidebar-collapsed" : ""}`}>
      <button
        className="sidebar-toggle"
        type="button"
        aria-label={sidebarCollapsed ? "Show sidebar" : "Hide sidebar"}
        aria-expanded={!sidebarCollapsed}
        onClick={() => setSidebarCollapsed((collapsed) => !collapsed)}
      >
        <span />
        <span />
        <span />
      </button>
      <Sidebar
        sidebar={sidebar}
        activeChatId={activeChat?.id ?? null}
        selectedProjectId={selectedProjectId}
        showNewProjectForm={showNewProjectForm}
        onToggleProjectForm={() => setShowNewProjectForm((visible) => !visible)}
        onCreateProject={handleCreateProject}
        onNewChat={handleNewChat}
        onOpenChat={handleOpenChat}
        onDeleteChat={handleDeleteChat}
        onDeleteProject={handleDeleteProject}
        onOpenSettings={() => setShowSettings(true)}
      />

      <main className="neo-main">
        <section className="neo-shell">
          {showEmptyState && (
            <div className="neo-empty-state">
              <h1 className="neo-title">Neo</h1>
              <p className="neo-subtitle">Your local personal AI assistant</p>
            </div>
          )}

          {messages.map((message) => (
            <ChatMessage
              key={message.id}
              message={message}
              messages={messages}
              editingMessageId={editingMessageId}
              editingValue={editingValue}
              onCancelEdit={() => {
                setEditingMessageId(null);
                setEditingValue("");
              }}
              onCopy={copyText}
              onEdit={handleEditMessage}
              onRerun={(prompt) => sendPrompt(prompt)}
              onSaveEdit={handleSaveEditedMessage}
              onSetEditingValue={setEditingValue}
              onToggleThinking={(messageId) =>
                setOpenThinkingMessageId((current) => (current === messageId ? null : messageId))
              }
              thinkingOpen={openThinkingMessageId === message.id}
            />
          ))}

          {sending && <PendingAssistantMessage generation={streamingAssistant} elapsedMs={elapsedMs} />}

          {showEmptyState && (
            <div className="neo-status">
              <span className="neo-pill">READY</span>
              Start a conversation or open a previous chat from the sidebar.
            </div>
          )}

          {statusError && <div className="neo-error">{statusError}</div>}
        </section>

        <ChatComposer
          value={composerValue}
          onChange={setComposerValue}
          onSubmit={handleSendMessage}
          disabled={sending}
        />
      </main>

      {showSettings && (
        <SettingsDialog
          onOpenMemory={() => {
            setShowSettings(false);
            setShowMemory(true);
          }}
          onClose={() => setShowSettings(false)}
        />
      )}

      {showMemory && (
        <MemoryDialog
          refreshSidebar={refreshSidebar}
          onClose={() => {
            setShowMemory(false);
          }}
        />
      )}

      <ConfirmDeleteDialog
        pendingDelete={pendingDelete}
        onCancel={() => setPendingDelete(null)}
        onConfirm={confirmDeletion}
      />
    </div>
  );
}
