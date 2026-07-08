import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { api } from "./api.js";
import Notes from "./Notes.jsx";
import Projects from "./Projects.jsx";
import Research from "./Research.jsx";
import Tasks from "./Tasks.jsx";
import Files from "./Files.jsx";
import Repos from "./Repos.jsx";
import CodingAgent from "./CodingAgent.jsx";

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
const MEMORY_SORT_OPTIONS = [
  ["newest", "Newest First"],
  ["oldest", "Oldest First"],
  ["az", "A \u2192 Z"],
  ["za", "Z \u2192 A"],
];
const ACTIVE_AGENT_RUN_STATUSES = new Set(["queued", "planning", "running", "waiting_approval"]);

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

function formatAgentStatus(value) {
  if (value === "waiting_approval") return "Waiting for approval";
  return String(value || "").replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatAgentTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function createdTime(record) {
  const parsed = Date.parse(record.created_at ?? "");
  return Number.isNaN(parsed) ? 0 : parsed;
}

function memorySortText(record) {
  return String(record.memory_text ?? "");
}

function sortMemoryRecords(records, sortOrder) {
  return [...records].sort((left, right) => {
    if (sortOrder === "oldest") {
      return createdTime(left) - createdTime(right);
    }
    if (sortOrder === "az") {
      return memorySortText(left).localeCompare(memorySortText(right), undefined, {
        sensitivity: "base",
      });
    }
    if (sortOrder === "za") {
      return memorySortText(right).localeCompare(memorySortText(left), undefined, {
        sensitivity: "base",
      });
    }
    return createdTime(right) - createdTime(left);
  });
}

function parseQueryId(params, key) {
  const value = params.get(key);
  if (!value) {
    return null;
  }
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}

function parsePermalink(pathname = window.location.pathname) {
  const chatMatch = pathname.match(/^\/chats\/(\d+)\/?$/);
  if (chatMatch) {
    return { type: "chat", id: Number(chatMatch[1]) };
  }
  const projectMatch = pathname.match(/^\/projects\/([^/]+)\/?$/);
  if (projectMatch) {
    return { type: "project", id: decodeURIComponent(projectMatch[1]) };
  }
  if (/^\/projects\/?$/.test(pathname)) {
    return { type: "projects", id: null };
  }
  return null;
}

function updatePermalink(path, { replace = false } = {}) {
  const method = replace ? "replaceState" : "pushState";
  if (`${window.location.pathname}${window.location.search}` !== path) {
    window.history[method]({}, "", path);
  }
}

function chatPermalink(chatId) {
  return `/chats/${chatId}`;
}

function projectPermalink(projectId) {
  return projectId ? `/projects/${encodeURIComponent(projectId)}` : "/projects";
}

function handlePermalinkClick(event, open) {
  if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
    return;
  }
  event.preventDefault();
  open();
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
  onOpenChatHome,
  onOpenMemory,
  onOpenResearch,
  onOpenNotes,
  onOpenProjects,
  onOpenTasks,
  onOpenFiles,
  onOpenRepos,
}) {
  const [projectName, setProjectName] = useState("");
  const [projectsCollapsed, setProjectsCollapsed] = useState(false);

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
      <nav className="sidebar-workspace-nav" aria-label="Workspace">
        <button type="button" onClick={onOpenChatHome}>Chat</button>
        <button type="button" onClick={onOpenMemory}>Memory</button>
        <button type="button" onClick={onOpenResearch}>Research</button>
        <button type="button" onClick={onOpenNotes}>Notes</button>
        <button type="button" onClick={onOpenProjects}>Projects</button>
        <button type="button" onClick={onOpenTasks}>Tasks</button>
        <button type="button" onClick={onOpenFiles}>Files</button>
        <button type="button" onClick={onOpenRepos}>Repos</button>
      </nav>
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

      <div className="sidebar-section sidebar-section-row">
        <span>Projects</span>
        <button
          className="sidebar-section-toggle"
          type="button"
          aria-label={projectsCollapsed ? "Show projects" : "Hide projects"}
          title={projectsCollapsed ? "Show projects" : "Hide projects"}
          onClick={() => setProjectsCollapsed((collapsed) => !collapsed)}
        >
          {projectsCollapsed ? "+" : "-"}
        </button>
      </div>
      {projectsCollapsed ? null : sidebar.projects.length === 0 ? (
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
              <a
                key={chat.id}
                className={`project-chat-link ${chat.id === activeChatId ? "active" : ""}`}
                href={chatPermalink(chat.id)}
                onClick={(event) => handlePermalinkClick(event, () => onOpenChat(chat.id))}
              >
                {chat.title}
              </a>
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
            <a
              className="chat-item-title"
              href={chatPermalink(chat.id)}
              onClick={(event) => handlePermalinkClick(event, () => onOpenChat(chat.id))}
            >
              {chat.title}
            </a>
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
          {hasThinking ? generation.thinking : "Waiting for response..."}
        </div>
        {hasContent && <div className="chat-content live-answer">{generation.content}</div>}
      </div>
    </article>
  );
}

function ChatComposer({
  disabled,
  value,
  onChange,
  onSubmit,
  llms,
  llmId,
  onLlmChange,
  mode,
  onModeChange,
  tasks,
  tasksLoading,
  selectedTaskId,
  onTaskChange,
  projects,
  selectedProjectId,
  onProjectChange,
  onPlanAgentTasks,
  planningTasks,
  proposedPlan,
  onCreatePlannedTasks,
  onCreatePlannedTasksAndRun,
  onCancelPlan,
  createdTasks,
  agentRun,
  agentMessage,
  agentDetailsOpen,
  onToggleAgentDetails,
  onOpenAgentTask,
  onSaveAgentRun,
}) {
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
    <div className={`chat-input-wrap ${mode === "agent" ? "agent-mode" : "chatbot-mode"}`}>
      <div className="chat-input-shell">
        <div className="chat-mode-row">
          <div className="chat-mode-switch" role="tablist" aria-label="Interaction mode">
            <button type="button" role="tab" aria-selected={mode === "chatbot"}
              className={mode === "chatbot" ? "active" : ""} onClick={() => onModeChange("chatbot")}>Chatbot</button>
            <button type="button" role="tab" aria-selected={mode === "agent"}
              className={mode === "agent" ? "active" : ""} onClick={() => onModeChange("agent")}>Agent</button>
          </div>
          {mode === "chatbot" ? (
            <div className="chat-llm-picker">
              <select
                value={llmId || ""}
                onChange={(event) => onLlmChange(event.target.value)}
                disabled={disabled}
                aria-label="Choose LLM"
              >
                {llms.filter((llm) => llm.enabled).map((llm) => (
                  <option key={llm.id} value={llm.id}>{llm.name} / {llm.model}</option>
                ))}
              </select>
            </div>
          ) : (
            <div className="agent-context-pickers">
              <label className="agent-task-picker">
                <span>Project</span>
                <select value={selectedProjectId} onChange={(event) => onProjectChange(event.target.value)}
                  disabled={disabled || tasksLoading} aria-label="Select optional project for agent">
                  <option value="">Optional project</option>
                  {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
                </select>
              </label>
              <label className="agent-task-picker">
                <span>Task</span>
                <select value={selectedTaskId} onChange={(event) => onTaskChange(event.target.value)}
                  disabled={disabled || tasksLoading} aria-label="Select optional existing task for agent">
                  <option value="">{tasksLoading ? "Loading tasks…" : "Optional existing task"}</option>
                  {tasks.map((task) => <option key={task.id} value={task.id}>{task.title} · {task.status}</option>)}
                </select>
              </label>
            </div>
          )}
        </div>
        <form className="chat-input-form" onSubmit={onSubmit}>
          <textarea
            ref={textareaRef}
            value={value}
            onChange={(event) => {
              onChange(event.target.value);
              requestAnimationFrame(resizeComposer);
            }}
            onInput={resizeComposer}
            placeholder={mode === "agent" ? "What should the agent work on?" : "Message Neo"}
            rows={1}
            disabled={disabled}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                event.currentTarget.form?.requestSubmit();
              }
            }}
          />
          {mode === "agent" ? (
            <div className="agent-submit-actions">
              <button type="button" className="neo-button secondary" onClick={onPlanAgentTasks}
                disabled={disabled || planningTasks || !value.trim()}>Plan Tasks</button>
              <NeoButton type="submit" className="agent-run-button"
                disabled={disabled || (!selectedTaskId && !value.trim())}
                aria-label="Run Agent" title="Run Agent">Run Agent</NeoButton>
            </div>
          ) : (
            <NeoButton type="submit" className="send-button" disabled={disabled || !value.trim()}
              aria-label="Send message" title="Send message">{"\u2191"}</NeoButton>
          )}
        </form>
        {mode === "agent" && !selectedTaskId && !value.trim() && !tasksLoading ? (
          <div className="agent-mode-hint">Select an existing task or enter an objective.</div>
        ) : null}
        {mode === "agent" && agentMessage ? <div className="agent-mode-message">{agentMessage}</div> : null}
        {mode === "agent" && proposedPlan ? (
          <div className="agent-plan-preview">
            <div className="agent-plan-preview-head">
              <div><strong>{proposedPlan.parent_task.title}</strong><span>{proposedPlan.subtasks.length} proposed subtasks</span></div>
              <button type="button" onClick={onCancelPlan}>Cancel</button>
            </div>
            <ol>{proposedPlan.subtasks.map((task) => <li key={task.order}><strong>{task.title}</strong><span>{task.description}</span></li>)}</ol>
            <div className="agent-plan-actions">
              <button type="button" onClick={onCreatePlannedTasks} disabled={disabled}>Create Tasks</button>
              <button type="button" onClick={onCreatePlannedTasksAndRun} disabled={disabled}>Create Tasks &amp; Run Agent</button>
            </div>
          </div>
        ) : null}
        {mode === "agent" && createdTasks?.length ? (
          <div className="agent-created-tasks">
            <strong>Created {createdTasks.length} tasks</strong>
            <span>{createdTasks[0].title} with {Math.max(0, createdTasks.length - 1)} subtasks.</span>
          </div>
        ) : null}
        {mode === "agent" && agentRun ? (
          <div className="chat-agent-status" aria-live="polite">
            <div className="chat-agent-status-main">
              <div>
                <strong>{agentRun.run.title}</strong>
                <span>{formatAgentTime(agentRun.run.created_at)}</span>
              </div>
              <span className={`agent-status ${agentRun.run.status}`}>{formatAgentStatus(agentRun.run.status)}</span>
            </div>
            <div className="chat-agent-actions">
              <button type="button" onClick={() => onOpenAgentTask(agentRun.run.task_id)}>Open Task</button>
              <button type="button" onClick={onToggleAgentDetails}>{agentDetailsOpen ? "Hide Run" : "Open Run"}</button>
              {agentRun.run.status === "completed" ? (
                <button type="button" onClick={onSaveAgentRun} disabled={disabled}>Save Output to Note</button>
              ) : null}
            </div>
            {agentDetailsOpen ? (
              <div className="chat-agent-details">
                {agentRun.steps.map((step) => (
                  <div key={step.id}><span>{step.title}</span><span>{formatAgentStatus(step.status)}</span></div>
                ))}
                {agentRun.run.error ? <div className="chat-agent-error">{agentRun.run.error}</div> : null}
                {agentRun.run.final_output ? <pre>{agentRun.run.final_output}</pre> : null}
              </div>
            ) : null}
          </div>
        ) : null}
        {mode === "agent" ? <CodingAgent initialTaskId={selectedTaskId} initialProjectId={selectedProjectId} compact /> : null}
      </div>
      <div className="chat-input-disclaimer">
        {mode === "agent"
          ? "Agent runs are task-linked and audited. No chat message is sent in Agent mode."
          : "Neo is an AI and it can make mistakes. Please double-check responses."}
      </div>
    </div>
  );
}

function WebSearchSettingsDialog({ onClose }) {
  const [searchConfig, setSearchConfig] = useState(null);
  const [provider, setProvider] = useState("searxng");
  const [searxngInstance, setSearxngInstance] = useState("http://localhost:8080");
  const [tavilyKey, setTavilyKey] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function loadSearchConfig() {
      setLoading(true);
      setError("");
      try {
        const config = await api.searchConfig();
        if (cancelled) {
          return;
        }
        setSearchConfig(config);
        setProvider(config.provider || "searxng");
        setSearxngInstance(config.searxng_instance || "http://localhost:8080");
      } catch (requestError) {
        if (!cancelled) {
          setError(errorMessage(requestError));
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    loadSearchConfig();
    return () => {
      cancelled = true;
    };
  }, []);

  async function saveSearchConfig(event) {
    event.preventDefault();
    setSaving(true);
    setStatus("");
    setError("");
    try {
      const config = await api.updateSearchConfig({
        provider,
        searxng_instance: searxngInstance,
        tavily_key: provider === "tavily" ? tavilyKey : undefined,
      });
      setSearchConfig(config);
      setProvider(config.provider || "searxng");
      setSearxngInstance(config.searxng_instance || "http://localhost:8080");
      setTavilyKey("");
      setStatus("Saved.");
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setSaving(false);
    }
  }

  async function testSearchConfig() {
    setTesting(true);
    setStatus("");
    setError("");
    try {
      const result = await api.testSearchProvider({ query: "latest OpenAI news" });
      if (!result.success) {
        setError(result.error || "Search test failed.");
        return;
      }
      setStatus(
        `Test passed: ${result.provider_used} returned ${result.result_count} result(s) in ${result.latency_ms} ms.`,
      );
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setTesting(false);
    }
  }

  return (
    <Modal title="Web Search" onClose={onClose} className="settings-dialog web-search-dialog">
      <section className="settings-section">
        {loading ? (
          <p className="dialog-caption">Loading...</p>
        ) : (
          <form onSubmit={saveSearchConfig}>
            <Field label="Provider">
              <select value={provider} onChange={(event) => setProvider(event.target.value)}>
                <option value="searxng">SearXNG</option>
                <option value="tavily">Tavily</option>
              </select>
            </Field>

            {provider === "searxng" && (
              <Field label="Instance URL">
                <input
                  value={searxngInstance}
                  onChange={(event) => setSearxngInstance(event.target.value)}
                  placeholder="http://localhost:8080"
                />
              </Field>
            )}

            {provider === "tavily" && (
              <Field label="API Key">
                <input
                  value={tavilyKey}
                  onChange={(event) => setTavilyKey(event.target.value)}
                  placeholder={searchConfig?.tavily_configured ? "Configured" : "TAVILY_API_KEY"}
                  type="password"
                  autoComplete="off"
                />
              </Field>
            )}

            <div className="settings-actions">
              <NeoButton type="submit" disabled={saving || testing}>
                {saving ? "Saving..." : "Save"}
              </NeoButton>
              <NeoButton type="button" disabled={saving || testing} onClick={testSearchConfig}>
                {testing ? "Testing..." : "Test"}
              </NeoButton>
            </div>
          </form>
        )}
        {error && <div className="neo-error">{error}</div>}
        {status && <div className="settings-status">{status}</div>}
      </section>
    </Modal>
  );
}

const EMPTY_LLM_FORM = {
  id: "",
  name: "",
  provider: "ollama",
  model: "",
  base_url: "http://127.0.0.1:11434",
  api_key: "",
  api_key_env: "",
  enabled: true,
  timeout_seconds: 240,
  num_predict: 512,
};

function LLMSettingsDialog({ onClose, onChanged }) {
  const [config, setConfig] = useState({ active_id: "", llms: [] });
  const [form, setForm] = useState(EMPTY_LLM_FORM);
  const [editingId, setEditingId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  const load = useCallback(async () => {
    const next = await api.llms();
    setConfig(next);
    onChanged(next);
    return next;
  }, [onChanged]);

  useEffect(() => {
    let cancelled = false;
    load()
      .catch((nextError) => {
        if (!cancelled) setError(errorMessage(nextError));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [load]);

  function updateField(key, value) {
    setForm((current) => {
      const next = { ...current, [key]: value };
      if (key === "provider" && !editingId) {
        next.base_url = value === "ollama" ? "http://127.0.0.1:11434" : "";
      }
      return next;
    });
  }

  function resetForm() {
    setEditingId(null);
    setForm(EMPTY_LLM_FORM);
  }

  function editLlm(llm) {
    setEditingId(llm.id);
    setForm({
      ...EMPTY_LLM_FORM,
      ...llm,
      api_key: "",
      api_key_env: llm.api_key_env || "",
    });
    setStatus("");
    setError("");
  }

  async function saveLlm(event) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setStatus("");
    try {
      const payload = {
        ...form,
        id: form.id.trim(),
        name: form.name.trim(),
        model: form.model.trim(),
        base_url: form.base_url.trim(),
        api_key_env: form.api_key_env.trim() || null,
        timeout_seconds: Number(form.timeout_seconds),
        num_predict: Number(form.num_predict),
      };
      if (!form.api_key) delete payload.api_key;
      const next = await api.saveLlm(payload);
      setConfig(next);
      onChanged(next);
      setStatus(editingId ? "LLM updated." : "LLM added.");
      resetForm();
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setSaving(false);
    }
  }

  async function selectLlm(id) {
    setError("");
    setStatus("");
    try {
      const next = await api.selectLlm(id);
      setConfig(next);
      onChanged(next);
      setStatus("Active LLM changed.");
    } catch (nextError) {
      setError(errorMessage(nextError));
    }
  }

  async function testLlm(id) {
    setTestingId(id);
    setError("");
    setStatus("");
    try {
      const result = await api.testLlm(id);
      setStatus(
        result.available && result.model_available
          ? "Connection and LLM check passed."
          : result.available
            ? "Server is reachable, but the configured LLM was not found."
            : "Server could not be reached.",
      );
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setTestingId(null);
    }
  }

  async function deleteLlm(id) {
    setError("");
    setStatus("");
    try {
      await api.deleteLlm(id);
      const next = await load();
      setConfig(next);
      if (editingId === id) resetForm();
      setStatus("LLM removed.");
    } catch (nextError) {
      setError(errorMessage(nextError));
    }
  }

  return (
    <Modal title="LLMs" onClose={onClose} wide className="llm-settings-dialog">
      <div className="llm-settings-layout">
        <section className="llm-config-list">
          <div className="llm-section-heading">Configured LLMs</div>
          {loading ? (
            <p className="dialog-caption">Loading...</p>
          ) : config.llms.length === 0 ? (
            <p className="dialog-caption">No LLMs configured.</p>
          ) : (
            config.llms.map((llm) => (
              <article className={`llm-config-card ${config.active_id === llm.id ? "active" : ""}`} key={llm.id}>
                <div className="llm-config-title">
                  <strong>{llm.name}</strong>
                  {config.active_id === llm.id && <span>Active</span>}
                </div>
                <div className="llm-config-meta">{llm.model}</div>
                <div className="llm-config-meta">{llm.provider === "ollama" ? "Local / Ollama" : "OpenAI-compatible / API or local"}</div>
                <div className="llm-card-actions">
                  {config.active_id !== llm.id && <NeoButton onClick={() => selectLlm(llm.id)}>Use</NeoButton>}
                  <NeoButton onClick={() => testLlm(llm.id)} disabled={testingId === llm.id}>
                    {testingId === llm.id ? "Testing..." : "Test"}
                  </NeoButton>
                  <NeoButton onClick={() => editLlm(llm)}>Edit</NeoButton>
                  <NeoButton className="danger" onClick={() => deleteLlm(llm.id)}>Delete</NeoButton>
                </div>
              </article>
            ))
          )}
        </section>

        <form className="llm-config-form" onSubmit={saveLlm}>
          <div className="llm-section-heading">{editingId ? "Edit LLM" : "Add LLM"}</div>
          <Field label="Connection type">
            <select value={form.provider} onChange={(event) => updateField("provider", event.target.value)}>
              <option value="ollama">Local / Ollama</option>
              <option value="openai_compatible">OpenAI-compatible / API or local</option>
            </select>
          </Field>
          <Field label="Configuration ID">
            <input value={form.id} onChange={(event) => updateField("id", event.target.value)} placeholder="my-llm" disabled={Boolean(editingId)} required />
          </Field>
          <Field label="Display name">
            <input value={form.name} onChange={(event) => updateField("name", event.target.value)} placeholder="My local LLM" required />
          </Field>
          <Field label="LLM identifier">
            <input value={form.model} onChange={(event) => updateField("model", event.target.value)} placeholder={form.provider === "ollama" ? "llama3.2:3b" : "provider-llm-name"} required />
          </Field>
          <Field label="Endpoint">
            <input value={form.base_url} onChange={(event) => updateField("base_url", event.target.value)} placeholder={form.provider === "ollama" ? "http://127.0.0.1:11434" : "https://provider.example/v1"} required />
          </Field>
          {form.provider === "openai_compatible" && (
            <>
              <Field label="API key">
                <input type="password" autoComplete="off" value={form.api_key} onChange={(event) => updateField("api_key", event.target.value)} placeholder={editingId && config.llms.find((llm) => llm.id === editingId)?.has_api_key ? "Configured — leave blank to keep" : "Optional for local APIs"} />
              </Field>
              <Field label="API key environment variable">
                <input value={form.api_key_env} onChange={(event) => updateField("api_key_env", event.target.value)} placeholder="MY_LLM_API_KEY" />
              </Field>
            </>
          )}
          <div className="llm-number-fields">
            <Field label="Timeout (seconds)">
              <input type="number" min="1" max="3600" value={form.timeout_seconds} onChange={(event) => updateField("timeout_seconds", event.target.value)} />
            </Field>
            <Field label="Output token limit">
              <input type="number" min="1" max="32768" value={form.num_predict} onChange={(event) => updateField("num_predict", event.target.value)} />
            </Field>
          </div>
          <label className="llm-enabled-toggle">
            <input type="checkbox" checked={form.enabled} onChange={(event) => updateField("enabled", event.target.checked)} />
            Enabled
          </label>
          <div className="settings-actions">
            <NeoButton type="submit" disabled={saving}>{saving ? "Saving..." : editingId ? "Save changes" : "Add LLM"}</NeoButton>
            {editingId && <NeoButton type="button" onClick={resetForm}>Cancel</NeoButton>}
          </div>
        </form>
      </div>
      {error && <div className="neo-error">{error}</div>}
      {status && <div className="settings-status">{status}</div>}
    </Modal>
  );
}

function SettingsDialog({ onOpenLLMs, onOpenMemory, onOpenNotes, onOpenProjects, onOpenResearch, onOpenTasks, onOpenWebSearch, onClose }) {
  return (
    <Modal title="Settings" onClose={onClose} className="settings-dialog">
      <p className="dialog-caption">App controls</p>
      <div className="settings-menu">
        <NeoButton className="w-full" onClick={onOpenLLMs}>
          LLMs
        </NeoButton>
        <NeoButton className="w-full" onClick={onOpenResearch}>
          Research
        </NeoButton>
        <NeoButton className="w-full" onClick={onOpenWebSearch}>
          Web Search
        </NeoButton>
        <NeoButton className="w-full" onClick={onOpenMemory}>
          Memory
        </NeoButton>
        <NeoButton className="w-full" onClick={onOpenNotes}>
          Notes
        </NeoButton>
        <NeoButton className="w-full" onClick={onOpenProjects}>
          Projects
        </NeoButton>
        <NeoButton className="w-full" onClick={onOpenTasks}>
          Tasks
        </NeoButton>
      </div>
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
  const [memorySortOrder, setMemorySortOrder] = useState("newest");
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
      content = (
        <GeneralMemoryEditor
          records={sortMemoryRecords(data.memories, memorySortOrder)}
          {...editorProps}
        />
      );
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
      {activeTab === "memories" && (
        <div className="memory-sort-bar">
          <label>
            <span>Sort</span>
            <select
              value={memorySortOrder}
              onChange={(event) => setMemorySortOrder(event.target.value)}
            >
              {MEMORY_SORT_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}
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
  const [showLlmSettings, setShowLlmSettings] = useState(false);
  const [showWebSearchSettings, setShowWebSearchSettings] = useState(false);
  const [showMemory, setShowMemory] = useState(false);
  const [pendingDelete, setPendingDelete] = useState(null);
  const [composerValue, setComposerValue] = useState("");
  const [editingMessageId, setEditingMessageId] = useState(null);
  const [editingValue, setEditingValue] = useState("");
  const [openThinkingMessageId, setOpenThinkingMessageId] = useState(null);
  const [sending, setSending] = useState(false);
  const [streamingAssistant, setStreamingAssistant] = useState(null);
  const [generationChatId, setGenerationChatId] = useState(null);
  const [generationStartedAt, setGenerationStartedAt] = useState(null);
  const [elapsedMs, setElapsedMs] = useState(0);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [statusError, setStatusError] = useState("");
  const [llms, setLlms] = useState([]);
  const [selectedLlmId, setSelectedLlmId] = useState("");
  const [showResearch, setShowResearch] = useState(false);
  const [showNotes, setShowNotes] = useState(false);
  const [showProjects, setShowProjects] = useState(false);
  const [showTasks, setShowTasks] = useState(false);
  const [showFiles, setShowFiles] = useState(false);
  const [showRepos, setShowRepos] = useState(false);
  const [initialFileId, setInitialFileId] = useState(null);
  const [initialProjectId, setInitialProjectId] = useState(null);
  const [initialNoteId, setInitialNoteId] = useState(null);
  const [initialTaskId, setInitialTaskId] = useState(null);
  const [initialTaskProjectId, setInitialTaskProjectId] = useState(null);
  const [chatMode, setChatMode] = useState("chatbot");
  const [agentTasks, setAgentTasks] = useState([]);
  const [agentProjects, setAgentProjects] = useState([]);
  const [agentTasksLoading, setAgentTasksLoading] = useState(false);
  const [selectedAgentTaskId, setSelectedAgentTaskId] = useState("");
  const [selectedAgentProjectId, setSelectedAgentProjectId] = useState("");
  const [agentTaskPlan, setAgentTaskPlan] = useState(null);
  const [agentCreatedTasks, setAgentCreatedTasks] = useState([]);
  const [agentPlanning, setAgentPlanning] = useState(false);
  const [chatAgentRun, setChatAgentRun] = useState(null);
  const [chatAgentBusy, setChatAgentBusy] = useState(false);
  const [chatAgentMessage, setChatAgentMessage] = useState("");
  const [chatAgentDetailsOpen, setChatAgentDetailsOpen] = useState(false);
  const bootstrapped = useRef(false);
  const visibleChatIdRef = useRef(null);

  const refreshSidebar = useCallback(async () => {
    const nextSidebar = await api.sidebar();
    setSidebar(nextSidebar);
    return nextSidebar;
  }, []);

  const loadAgentContext = useCallback(async () => {
    setAgentTasksLoading(true);
    try {
      const [taskData, projectData] = await Promise.all([
        api.tasksList({ includeArchived: false, pinnedFirst: true, limit: 100 }),
        api.projectsList({ includeArchived: false, pinnedFirst: true, limit: 100 }),
      ]);
      setAgentTasks(taskData.tasks || []);
      setAgentProjects(projectData.projects || []);
    } catch (error) {
      setStatusError(`Could not load Agent mode context: ${errorMessage(error)}`);
    } finally {
      setAgentTasksLoading(false);
    }
  }, []);

  useEffect(() => {
    if (chatMode === "agent") loadAgentContext();
  }, [chatMode, loadAgentContext]);

  useEffect(() => {
    const runId = chatAgentRun?.run?.id;
    const status = chatAgentRun?.run?.status;
    if (!runId || !ACTIVE_AGENT_RUN_STATUSES.has(status)) return undefined;
    const interval = window.setInterval(async () => {
      try {
        const detail = await api.agentRun(runId);
        setChatAgentRun(detail);
        if (!ACTIVE_AGENT_RUN_STATUSES.has(detail.run.status)) {
          setChatAgentMessage(
            detail.run.status === "completed"
              ? "Agent run completed."
              : `Agent run ${formatAgentStatus(detail.run.status).toLowerCase()}.`,
          );
        }
      } catch (error) {
        setChatAgentMessage(`Could not refresh the agent run: ${errorMessage(error)}`);
      }
    }, 1000);
    return () => window.clearInterval(interval);
  }, [chatAgentRun?.run?.id, chatAgentRun?.run?.status]);

  const handleLlmConfigChanged = useCallback((next) => {
    setLlms(next.llms || []);
    setSelectedLlmId(next.active_id || "");
  }, []);

  const loadChat = useCallback(async (chatId, options = {}) => {
    const thread = await api.getChat(chatId);
    setActiveChat(thread.chat);
    setMessages(thread.messages);
    setSelectedProjectId(thread.chat.project_id);
    localStorage.setItem("neo-active-chat-id", String(thread.chat.id));
    if (options.history !== "none") {
      updatePermalink(chatPermalink(thread.chat.id), { replace: options.history === "replace" });
    }
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
      updatePermalink(chatPermalink(chat.id), { replace: options.history === "replace" });
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
        try {
          const llmConfig = await api.llms();
          setLlms(llmConfig.llms || []);
          setSelectedLlmId(llmConfig.active_id || "");
        } catch (error) {
          setStatusError(`Could not load LLM configurations: ${errorMessage(error)}`);
        }
        const params = new URLSearchParams(window.location.search);
        const permalink = parsePermalink();
        const openChatId = parseQueryId(params, "open_chat");
        const deleteChatId = parseQueryId(params, "request_delete_chat");
        const deleteProjectId = parseQueryId(params, "request_delete_project");
        const newProjectChatId = parseQueryId(params, "new_project_chat");
        const selectedProjectIdFromQuery = parseQueryId(params, "select_project");

        if (permalink?.type === "project" || permalink?.type === "projects") {
          setInitialProjectId(permalink.id);
          setShowProjects(true);
          clearSidebarQueryActions();
          return;
        }

        if (permalink?.type === "chat") {
          await loadChat(permalink.id, { history: "replace" });
          clearSidebarQueryActions();
          return;
        }

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
          await createActiveChat(newProjectChatId, { history: "replace" });
          clearSidebarQueryActions();
          return;
        }

        if (openChatId) {
          try {
            await loadChat(openChatId, { history: "replace" });
          } finally {
            clearSidebarQueryActions();
          }
          return;
        }

        const storedChatId = Number(localStorage.getItem("neo-active-chat-id"));
        if (storedChatId) {
          try {
            await loadChat(storedChatId, { history: "replace" });
            clearSidebarQueryActions();
            return;
          } catch {
            localStorage.removeItem("neo-active-chat-id");
          }
        }
        await createActiveChat(selectedProjectIdFromQuery, { history: "replace" });
        clearSidebarQueryActions();
      } catch (error) {
        setStatusError(errorMessage(error));
      }
    }

    bootstrap();
  }, [createActiveChat, loadChat, refreshSidebar]);

  useEffect(() => {
    async function restorePermalink() {
      const permalink = parsePermalink();
      if (permalink?.type === "chat") {
        setShowProjects(false);
        setShowTasks(false);
        setShowNotes(false);
        setShowResearch(false);
        await loadChat(permalink.id, { history: "none" });
      } else if (permalink?.type === "project" || permalink?.type === "projects") {
        setInitialProjectId(permalink.id);
        setShowNotes(false);
        setShowTasks(false);
        setShowResearch(false);
        setShowProjects(true);
      }
    }
    window.addEventListener("popstate", restorePermalink);
    return () => window.removeEventListener("popstate", restorePermalink);
  }, [loadChat]);

  useEffect(() => {
    if (!generationStartedAt) {
      return undefined;
    }
    const updateElapsed = () => setElapsedMs(Date.now() - generationStartedAt);
    updateElapsed();
    const intervalId = window.setInterval(updateElapsed, 100);
    return () => window.clearInterval(intervalId);
  }, [generationStartedAt]);

  useEffect(() => {
    visibleChatIdRef.current = showProjects || showTasks || showNotes || showResearch ? null : activeChat?.id ?? null;
  }, [activeChat?.id, showNotes, showProjects, showResearch, showTasks]);

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
    const previousChatId = activeChat?.id ?? null;
    visibleChatIdRef.current = null;
    try {
      setShowResearch(false);
      setShowNotes(false);
      setShowProjects(false);
      setShowTasks(false);
      setInitialProjectId(null);
      const chat = await createActiveChat(projectId);
      visibleChatIdRef.current = chat.id;
    } catch (error) {
      visibleChatIdRef.current = previousChatId;
      setStatusError(errorMessage(error));
    }
  }

  async function handleOpenChat(chatId) {
    setStatusError("");
    const previousChatId = activeChat?.id ?? null;
    visibleChatIdRef.current = null;
    try {
      setShowResearch(false);
      setShowNotes(false);
      setShowProjects(false);
      setShowTasks(false);
      setInitialProjectId(null);
      await loadChat(chatId);
      visibleChatIdRef.current = chatId;
    } catch (error) {
      visibleChatIdRef.current = previousChatId;
      setStatusError(errorMessage(error));
    }
  }

  async function handleProjectsBack() {
    setShowProjects(false);
    setInitialProjectId(null);
    if (activeChat?.id) {
      updatePermalink(chatPermalink(activeChat.id));
      return;
    }
    const storedChatId = Number(localStorage.getItem("neo-active-chat-id"));
    try {
      if (storedChatId) {
        await loadChat(storedChatId);
      } else {
        await createActiveChat(null);
      }
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
    let requestChatId = activeChat?.id ?? null;

    try {
      const chat = activeChat ?? (await createActiveChat(selectedProjectId, { resetMessages: false }));
      requestChatId = chat.id;
      setGenerationChatId(chat.id);
      let rawContent = "";
      await api.streamMessage(chat.id, prompt, (event) => {
        if (event.type === "chunk") {
          rawContent += event.content;
          if (visibleChatIdRef.current === chat.id) {
            setStreamingAssistant({
              rawContent,
              ...splitGeneratedText(rawContent),
            });
          }
        }
      }, selectedLlmId || null);
      if (visibleChatIdRef.current === chat.id) {
        await loadChat(chat.id, { history: "none" });
      }
      await refreshSidebar();
    } catch (error) {
      if (visibleChatIdRef.current === requestChatId) {
        setMessages((current) =>
          current.map((message) =>
            message.id === pendingId ? { ...message, failed: true } : message,
          ),
        );
        setComposerValue(prompt);
        setStatusError(`${errorMessage(error)}. Your message was not sent, but it was kept.`);
      }
    } finally {
      setSending(false);
      setGenerationStartedAt(null);
      setGenerationChatId(null);
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

  async function handleStartChatAgent(event) {
    event.preventDefault();
    const objective = composerValue.trim();
    if ((!selectedAgentTaskId && !objective) || chatAgentBusy || agentPlanning) {
      if (!selectedAgentTaskId && !objective) setChatAgentMessage("Select an existing task or enter an objective.");
      return;
    }
    setChatAgentBusy(true);
    setChatAgentMessage("");
    setStatusError("");
    try {
      let created;
      if (selectedAgentTaskId) {
        created = await api.startAgentRun({
          task_id: selectedAgentTaskId,
          objective: objective || null,
          mode: "assist",
        });
      } else {
        const result = await api.startAgentRunFromObjective({
          objective,
          project_id: selectedAgentProjectId || null,
          mode: "assist",
          auto_create_tasks: true,
        });
        created = { run: result.run };
        setSelectedAgentTaskId(result.parent_task.id);
        setAgentCreatedTasks([result.parent_task, ...result.subtasks]);
        setAgentTaskPlan(null);
        await loadAgentContext();
      }
      setComposerValue("");
      setChatAgentDetailsOpen(false);
      setChatAgentRun(await api.agentRun(created.run.id));
      setChatAgentMessage("Agent run started.");
    } catch (error) {
      setChatAgentMessage(`Could not start the agent run: ${errorMessage(error)}`);
    } finally {
      setChatAgentBusy(false);
    }
  }

  async function handlePlanAgentTasks() {
    const objective = composerValue.trim();
    if (!objective || agentPlanning || chatAgentBusy) {
      if (!objective) setChatAgentMessage("Enter an objective to plan tasks.");
      return;
    }
    setAgentPlanning(true);
    setChatAgentMessage("Planning tasks…");
    setAgentCreatedTasks([]);
    try {
      const result = await api.planAgentTasks({
        objective,
        project_id: selectedAgentProjectId || null,
        dry_run: true,
      });
      setAgentTaskPlan(result.plan);
      setChatAgentMessage("Task plan ready for review. No tasks were created.");
    } catch (error) {
      setChatAgentMessage(`Could not plan tasks: ${errorMessage(error)}`);
    } finally {
      setAgentPlanning(false);
    }
  }

  async function handleCreatePlannedTasks() {
    const objective = composerValue.trim();
    if (!objective || chatAgentBusy) return;
    setChatAgentBusy(true);
    try {
      const result = await api.planAgentTasks({
        objective,
        project_id: selectedAgentProjectId || null,
        dry_run: false,
      });
      setAgentCreatedTasks(result.tasks || []);
      setSelectedAgentTaskId(result.tasks?.[0]?.id || "");
      setAgentTaskPlan(null);
      setChatAgentMessage(`Created ${result.tasks?.length || 0} tasks. The parent task is selected.`);
      await loadAgentContext();
    } catch (error) {
      setChatAgentMessage(`Could not create tasks: ${errorMessage(error)}`);
    } finally {
      setChatAgentBusy(false);
    }
  }

  async function handleCreatePlannedTasksAndRun() {
    const objective = composerValue.trim();
    if (!objective || chatAgentBusy) return;
    setChatAgentBusy(true);
    try {
      const result = await api.startAgentRunFromObjective({
        objective,
        project_id: selectedAgentProjectId || null,
        mode: "assist",
        auto_create_tasks: true,
      });
      setAgentCreatedTasks([result.parent_task, ...result.subtasks]);
      setSelectedAgentTaskId(result.parent_task.id);
      setAgentTaskPlan(null);
      setComposerValue("");
      setChatAgentDetailsOpen(false);
      setChatAgentRun(await api.agentRun(result.run.id));
      setChatAgentMessage("Tasks created and Agent run started.");
      await loadAgentContext();
    } catch (error) {
      setChatAgentMessage(`Could not create tasks and run: ${errorMessage(error)}`);
    } finally {
      setChatAgentBusy(false);
    }
  }

  function handleComposerSubmit(event) {
    if (chatMode === "agent") return handleStartChatAgent(event);
    return handleSendMessage(event);
  }

  async function handleSaveChatAgentRun() {
    if (!chatAgentRun || chatAgentBusy) return;
    setChatAgentBusy(true);
    try {
      const saved = await api.saveAgentRunToNote(chatAgentRun.run.id, { tags: ["agent", "task-output"] });
      setChatAgentRun(await api.agentRun(chatAgentRun.run.id));
      setChatAgentMessage(saved.already_saved ? "Output was already saved to this Note." : "Output saved to Note.");
    } catch (error) {
      setChatAgentMessage(`Could not save the output: ${errorMessage(error)}`);
    } finally {
      setChatAgentBusy(false);
    }
  }

  function openAgentTask(taskId) {
    setInitialTaskId(taskId);
    setInitialTaskProjectId(null);
    setShowResearch(false);
    setShowNotes(false);
    setShowProjects(false);
    setShowTasks(true);
    setShowFiles(false);
    setShowRepos(false);
  }

  function openWorkspaceFile(fileId) {
    setInitialFileId(fileId);
    setShowResearch(false); setShowNotes(false); setShowProjects(false); setShowTasks(false); setShowRepos(false); setShowFiles(true);
  }

  async function handleLlmChange(llmId) {
    const previous = selectedLlmId;
    setSelectedLlmId(llmId);
    try {
      const config = await api.selectLlm(llmId);
      setLlms(config.llms || []);
      setSelectedLlmId(config.active_id);
    } catch (error) {
      setSelectedLlmId(previous);
      setStatusError(errorMessage(error));
    }
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
        onOpenChatHome={() => {
          setShowResearch(false); setShowNotes(false); setShowProjects(false); setShowTasks(false); setShowFiles(false); setShowRepos(false);
        }}
        onOpenMemory={() => setShowMemory(true)}
        onOpenResearch={() => {
          setShowNotes(false); setShowProjects(false); setShowTasks(false); setShowFiles(false); setShowRepos(false); setShowResearch(true);
        }}
        onOpenNotes={() => {
          setInitialNoteId(null); setShowResearch(false); setShowProjects(false); setShowTasks(false); setShowFiles(false); setShowRepos(false); setShowNotes(true);
        }}
        onOpenProjects={() => {
          setInitialProjectId(null); setShowResearch(false); setShowNotes(false); setShowTasks(false); setShowFiles(false); setShowRepos(false); setShowProjects(true);
        }}
        onOpenTasks={() => {
          setInitialTaskId(null); setInitialTaskProjectId(null); setShowResearch(false); setShowNotes(false); setShowProjects(false); setShowFiles(false); setShowRepos(false); setShowTasks(true);
        }}
        onOpenFiles={() => {
          setInitialFileId(null); setShowResearch(false); setShowNotes(false); setShowProjects(false); setShowTasks(false); setShowRepos(false); setShowFiles(true);
        }}
        onOpenRepos={() => {
          setShowResearch(false); setShowNotes(false); setShowProjects(false); setShowTasks(false); setShowFiles(false); setShowRepos(true);
        }}
      />

      {showProjects ? (
        <Projects
          initialProjectId={initialProjectId}
          onBack={handleProjectsBack}
          onProjectChange={(projectId, options = {}) => {
            setInitialProjectId(projectId);
            updatePermalink(projectPermalink(projectId), options);
          }}
          onOpenNote={(noteId) => {
            setInitialNoteId(noteId);
            setShowProjects(false);
            setShowResearch(false);
            setShowNotes(true);
          }}
          onOpenTask={(taskId) => {
            setInitialTaskId(taskId);
            setInitialTaskProjectId(null);
            setShowProjects(false);
            setShowResearch(false);
            setShowNotes(false);
            setShowTasks(true);
          }}
          onOpenFile={openWorkspaceFile}
        />
      ) : showTasks ? (
        <Tasks
          initialTaskId={initialTaskId}
          initialProjectId={initialTaskProjectId}
          onBack={() => { setShowTasks(false); setInitialTaskId(null); setInitialTaskProjectId(null); }}
          onTaskChange={setInitialTaskId}
          onOpenNote={(noteId) => {
            setInitialNoteId(noteId); setShowTasks(false); setShowProjects(false); setShowResearch(false); setShowNotes(true);
          }}
          onOpenFile={openWorkspaceFile}
        />
      ) : showNotes ? (
        <Notes
          initialNoteId={initialNoteId}
          onBack={() => {
            setShowNotes(false);
            setInitialNoteId(null);
          }}
          onOpenTask={(taskId) => {
            setInitialTaskId(taskId); setInitialTaskProjectId(null); setShowNotes(false); setShowProjects(false); setShowResearch(false); setShowTasks(true);
          }}
          onOpenFile={openWorkspaceFile}
        />
      ) : showFiles ? (
        <Files initialFileId={initialFileId} onBack={() => { setShowFiles(false); setInitialFileId(null); }} />
      ) : showRepos ? (
        <Repos onBack={() => setShowRepos(false)} onOpenFile={(fileId) => { setInitialFileId(fileId); setShowRepos(false); setShowFiles(true); }} />
      ) : showResearch ? (
        <Research
          onBack={() => setShowResearch(false)}
          onOpenNote={(noteId) => {
            setInitialNoteId(noteId);
            setShowResearch(false);
            setShowNotes(true);
          }}
        />
      ) : (
      <main className={`neo-main ${chatMode === "agent" ? "agent-chat-mode" : ""}`}>
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

          {sending && generationChatId === activeChat?.id && (
            <PendingAssistantMessage generation={streamingAssistant} elapsedMs={elapsedMs} />
          )}

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
          onSubmit={handleComposerSubmit}
          disabled={chatMode === "chatbot"
            ? sending
            : chatAgentBusy || agentPlanning || ACTIVE_AGENT_RUN_STATUSES.has(chatAgentRun?.run?.status)}
          llms={llms}
          llmId={selectedLlmId}
          onLlmChange={handleLlmChange}
          mode={chatMode}
          onModeChange={setChatMode}
          tasks={agentTasks}
          tasksLoading={agentTasksLoading}
          selectedTaskId={selectedAgentTaskId}
          onTaskChange={(taskId) => { setSelectedAgentTaskId(taskId); setAgentTaskPlan(null); }}
          projects={agentProjects}
          selectedProjectId={selectedAgentProjectId}
          onProjectChange={(projectId) => { setSelectedAgentProjectId(projectId); setAgentTaskPlan(null); }}
          onPlanAgentTasks={handlePlanAgentTasks}
          planningTasks={agentPlanning}
          proposedPlan={agentTaskPlan}
          onCreatePlannedTasks={handleCreatePlannedTasks}
          onCreatePlannedTasksAndRun={handleCreatePlannedTasksAndRun}
          onCancelPlan={() => setAgentTaskPlan(null)}
          createdTasks={agentCreatedTasks}
          agentRun={chatAgentRun}
          agentMessage={chatAgentMessage}
          agentDetailsOpen={chatAgentDetailsOpen}
          onToggleAgentDetails={() => setChatAgentDetailsOpen((open) => !open)}
          onOpenAgentTask={openAgentTask}
          onSaveAgentRun={handleSaveChatAgentRun}
        />
      </main>
      )}

      {showSettings && (
        <SettingsDialog
          onOpenLLMs={() => {
            setShowSettings(false);
            setShowLlmSettings(true);
          }}
          onOpenWebSearch={() => {
            setShowSettings(false);
            setShowWebSearchSettings(true);
          }}
          onOpenMemory={() => {
            setShowSettings(false);
            setShowMemory(true);
          }}
          onOpenResearch={() => {
            setShowSettings(false);
            setShowNotes(false);
            setShowProjects(false);
            setShowTasks(false);
            setShowResearch(true);
          }}
          onOpenNotes={() => {
            setShowSettings(false);
            setInitialNoteId(null);
            setShowResearch(false);
            setShowProjects(false);
            setShowTasks(false);
            setShowNotes(true);
          }}
          onOpenProjects={() => {
            setShowSettings(false);
            setShowResearch(false);
            setShowNotes(false);
            setShowTasks(false);
            setInitialProjectId(null);
            setShowProjects(true);
            updatePermalink(projectPermalink(null));
          }}
          onOpenTasks={() => {
            setShowSettings(false);
            setInitialTaskId(null);
            setInitialTaskProjectId(null);
            setShowResearch(false);
            setShowNotes(false);
            setShowProjects(false);
            setShowTasks(true);
          }}
          onClose={() => setShowSettings(false)}
        />
      )}

      {showLlmSettings && (
        <LLMSettingsDialog
          onClose={() => setShowLlmSettings(false)}
          onChanged={handleLlmConfigChanged}
        />
      )}

      {showWebSearchSettings && (
        <WebSearchSettingsDialog onClose={() => setShowWebSearchSettings(false)} />
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
