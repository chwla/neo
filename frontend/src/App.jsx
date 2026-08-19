import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";

import { api } from "./api.js";
import Notes from "./Notes.jsx";
import WorkspaceIcon from "./WorkspaceIcon.jsx";
import Projects from "./Projects.jsx";
import Research from "./Research.jsx";
import Tasks from "./Tasks.jsx";
import Files from "./Files.jsx";
import Repos from "./Repos.jsx";
import CodingAgent from "./CodingAgent.jsx";
import RulesProfiles from "./RulesProfiles.jsx";
import RecoveryPanel from "./RecoveryPanel.jsx";
import AgentSettings from "./AgentSettings.jsx";
import ToolsSkillsSettings from "./ToolsSkillsSettings.jsx";
import Bundles from "./Bundles.jsx";
import GitHub from "./GitHub.jsx";
import ContextMemory from "./ContextMemory.jsx";
import CommandSandbox from "./CommandSandbox.jsx";
import LspPanel from "./LspPanel.jsx";
import AgenticRuns from "./AgenticRuns.jsx";
import WebSearch from "./WebSearch.jsx";
import MemoryRetrieval from "./MemoryRetrieval.jsx";
import ProviderRuntime from "./ProviderRuntime.jsx";
import EvaluationHarness from "./EvaluationHarness.jsx";
import WorkspaceOrchestration from "./WorkspaceOrchestration.jsx";
import Continuity from "./Continuity.jsx";
import AccountSettings from "./AccountSettings.jsx";
import ProfilePicker from "./ProfilePicker.jsx";
import MemoryDialog from "./MemoryDialog.jsx";
import {
  formatDuration,
  formatResponseKind,
  formatTokens,
  renderMessageHtml,
  splitGeneratedText,
} from "./chatPresentation.js";

const EMPTY_SIDEBAR = { projects: [], chats: [] };
const ACTIVE_AGENT_RUN_STATUSES = new Set(["queued", "planning", "running", "waiting_approval"]);


function errorMessage(error) {
  if (!error) {
    return "";
  }
  return error.message || String(error);
}

function clientRequestId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function browserChatContext() {
  let timezone = null;
  try {
    timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || null;
  } catch {
    timezone = null;
  }
  return {
    timezone,
    locale: navigator.language || null,
  };
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

function parseNeoTimestamp(value) {
  const raw = String(value || "").trim();
  if (!raw) return Number.NaN;
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? raw : `${raw}Z`;
  return Date.parse(normalized);
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
  const projectChatMatch = pathname.match(/^\/projects\/(\d+)\/chat\/(\d+)\/?$/);
  if (projectChatMatch) {
    return {
      type: "projectChat",
      projectId: Number(projectChatMatch[1]),
      id: Number(projectChatMatch[2]),
    };
  }
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

function chatPermalink(chatId, projectId = null) {
  return projectId ? `/projects/${encodeURIComponent(projectId)}/chat/${chatId}` : `/chats/${chatId}`;
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

function NeoLogo() {
  return (
    <span className="neo-logo-mark" aria-hidden="true">
      <span className="neo-logo-inner">
        <span className="neo-logo-stem" />
        <span className="neo-logo-dot neo-logo-dot-top" />
        <span className="neo-logo-dot neo-logo-dot-bottom" />
      </span>
    </span>
  );
}

function NavIcon({ name }) {
  const paths = {
    chat: ["M4 5h16v11H8l-4 4V5Z"],
    memory: ["M4 6c0-1.66 3.58-3 8-3s8 1.34 8 3", "M4 6v6c0 1.66 3.58 3 8 3s8-1.34 8-3V6", "M4 12v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6"],
    research: ["M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14z", "m21 21-4.3-4.3"],
    notes: ["M5 3h14v18H5z", "M8 8h8", "M8 12h8", "M8 16h5"],
    projects: ["M3 8a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2", "M3 8h18v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"],
    tasks: ["M5 4h14v16H5z", "m8 12 2 2 5-5"],
    files: ["M6 2h8l4 4v16H6z", "M14 2v5h5"],
    repos: ["M4 4h6l2 3h8v13H4z", "M8 12h8", "M8 16h5"],
    settings: ["M4 6h16", "M4 12h16", "M4 18h16"],
  };
  return (
    <svg className="system-nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {(paths[name] || paths.chat).map((path) => <path d={path} key={path} />)}
    </svg>
  );
}

/**
 * Labelled form control used across the settings dialogs. It was referenced in
 * several dialogs but never defined, which made LLM Providers and Web Search
 * throw `Field is not defined` and blank the whole app.
 */
function Field({ label, hint, children }) {
  return (
    <label className="neo-field">
      <span className="neo-field-label">{label}</span>
      {children}
      {hint ? <small className="neo-field-hint">{hint}</small> : null}
    </label>
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

/**
 * One chat in the sidebar, owning its own rename editing state.
 *
 * The two lists (loose chats and chats inside a project) differ only in class names,
 * so they share this row rather than growing two copies of the rename flow.
 */
function SidebarChatRow({ chat, href, isActive, classes, onOpenChat, onDeleteChat, onRenameChat }) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(chat.title);

  function startRename() {
    setDraft(chat.title);
    setRenaming(true);
  }

  function cancelRename() {
    setDraft(chat.title);
    setRenaming(false);
  }

  function submitRename(event) {
    event.preventDefault();
    const cleaned = draft.trim();
    // Closing an untouched field, or one emptied by mistake, keeps the old title
    // instead of reporting an error the user did not ask for.
    if (cleaned && cleaned !== chat.title) {
      onRenameChat(chat, cleaned);
    }
    setRenaming(false);
  }

  if (renaming) {
    return (
      <div className={`${classes.item} ${isActive ? "active" : ""}`} data-chat-id={chat.id}>
        <form className="chat-rename-form" onSubmit={submitRename}>
          <input
            className="chat-rename-input"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                event.preventDefault();
                cancelRename();
              }
            }}
            onBlur={submitRename}
            aria-label={`Rename chat ${chat.title}`}
            maxLength={120}
            autoFocus
          />
        </form>
      </div>
    );
  }

  return (
    <div className={`${classes.item} ${isActive ? "active" : ""}`} data-chat-id={chat.id}>
      <button
        className="chat-item-rename"
        type="button"
        title="Rename chat"
        aria-label={`Rename chat ${chat.title}`}
        onClick={startRename}
      >
        &#9998;
      </button>
      <a
        className={classes.link}
        href={href}
        onClick={(event) => handlePermalinkClick(event, () => onOpenChat(chat.id))}
      >
        {chat.title}
      </a>
      <button
        className={classes.delete}
        type="button"
        title="Delete chat"
        aria-label={`Delete chat ${chat.title}`}
        onClick={() => onDeleteChat(chat)}
      >
        X
      </button>
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
  onRenameChat,
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
  activeView,
  profile,
  onSwitchProfile,
}) {
  const [projectName, setProjectName] = useState("");
  const [projectsCollapsed, setProjectsCollapsed] = useState(false);
  const [search, setSearch] = useState("");

  function submitProject(event) {
    event.preventDefault();
    const cleaned = projectName.trim();
    if (!cleaned) {
      return;
    }
    onCreateProject(cleaned);
    setProjectName("");
  }

  const query = search.trim().toLowerCase();
  const filteredProjects = sidebar.projects
    .map((project) => ({
      ...project,
      chats: project.chats.filter((chat) => !query || chat.title.toLowerCase().includes(query)),
    }))
    .filter((project) => !query || project.name.toLowerCase().includes(query) || project.chats.length);
  const filteredChats = sidebar.chats.filter((chat) => !query || chat.title.toLowerCase().includes(query));
  const systemItems = [
    ["memory", "Memory", onOpenMemory],
    ["research", "Research", onOpenResearch],
    ["notes", "Notes", onOpenNotes],
    ["projects", "Projects", onOpenProjects],
    ["files", "Files", onOpenFiles],
    ["repos", "Repositories", onOpenRepos],
  ];

  return (
    <aside className="neo-sidebar">
      <button className="sidebar-brand" type="button" onClick={onOpenChatHome} aria-label="Open chat home">
        <NeoLogo />
        <span>neo</span>
      </button>
      <div className="sidebar-primary-action">
        <NeoButton className="w-full" onClick={() => onNewChat(selectedProjectId)}>
          + NEW CONVERSATION
        </NeoButton>
      </div>
      <label className="sidebar-search">
        <NavIcon name="research" />
        <input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search..." aria-label="Search conversations" />
      </label>

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
        <span>RECENT</span>
        <span className="sidebar-section-actions">
          <button className="sidebar-section-toggle" type="button" aria-label="Create project" title="Create project" onClick={onToggleProjectForm}>+</button>
          <button className="sidebar-section-toggle" type="button" aria-label={projectsCollapsed ? "Show projects" : "Hide projects"} title={projectsCollapsed ? "Show projects" : "Hide projects"} onClick={() => setProjectsCollapsed((collapsed) => !collapsed)}>{projectsCollapsed ? "+" : "−"}</button>
        </span>
      </div>
      {projectsCollapsed ? null : filteredProjects.length === 0 ? (
        <p className="sidebar-caption">No projects yet.</p>
      ) : (
        filteredProjects.map((project) => (
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
              <SidebarChatRow
                key={chat.id}
                chat={chat}
                href={chatPermalink(chat.id, project.id)}
                isActive={chat.id === activeChatId}
                classes={{
                  item: "project-chat-item",
                  link: "project-chat-link",
                  delete: "project-chat-delete",
                }}
                onOpenChat={onOpenChat}
                onDeleteChat={onDeleteChat}
                onRenameChat={onRenameChat}
              />
            ))}
          </details>
        ))
      )}

      <div className="sidebar-section">CHATS</div>
      {filteredChats.length === 0 ? (
        <p className="sidebar-caption">No chats yet.</p>
      ) : (
        filteredChats.map((chat) => (
          <SidebarChatRow
            key={chat.id}
            chat={chat}
            href={chatPermalink(chat.id)}
            isActive={chat.id === activeChatId}
            classes={{ item: "chat-item", link: "chat-item-title", delete: "chat-item-delete" }}
            onOpenChat={onOpenChat}
            onDeleteChat={onDeleteChat}
            onRenameChat={onRenameChat}
          />
        ))
      )}

      <div className="sidebar-spacer" />
      <div className="sidebar-section">SYSTEM</div>
      <nav className="system-nav" aria-label="Neo system">
        {systemItems.map(([id, label, onClick]) => (
          <button className={activeView === id ? "active" : ""} type="button" onClick={onClick} key={id}>
            <NavIcon name={id} />
            <span>{label}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-footer">
        <button className={activeView === "settings" ? "sidebar-settings active" : "sidebar-settings"} type="button" onClick={onOpenSettings}>
          <NavIcon name="settings" />
          <span>Settings</span>
        </button>
        <button className="sidebar-profile" type="button" onClick={onSwitchProfile} title="Log out and choose another profile" aria-label="Log out and choose another profile">
          {profile.avatar_data ? <img src={profile.avatar_data} alt="" /> : profile.username.slice(0, 1).toUpperCase()}
        </button>
      </div>
    </aside>
  );
}

function formatElapsedDuration(durationMs) {
  if (!Number.isFinite(durationMs)) {
    return "0.0 s";
  }
  const seconds = durationMs / 1000;
  return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
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
  const hasThinking = Boolean(message.thinking?.trim());
  const isEditing = isUser && editingMessageId === message.id;
  const previousUser = isUser ? null : previousUserMessage(messages, message);
  const metadataItems = isUser
    ? []
    : [formatResponseKind(message), formatTokens(message), formatDuration(message.duration_ms)]
      .filter(Boolean);

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
            {/* Escaped by renderMessageHtml before any tag is emitted. */}
            <div
              className="chat-content"
              dangerouslySetInnerHTML={{ __html: renderMessageHtml(message.content) }}
            />
            {message.failed && (
              <div className="chat-message-status">Not sent. Edit and try again.</div>
            )}
            {!isUser && metadataItems.length > 0 && (
              <div className="message-meta">
                {metadataItems.map((item) => <span key={item}>{item}</span>)}
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
                  <button
                    type="button"
                    title={
                      hasThinking
                        ? "Show the model reasoning returned for this response"
                        : "Explain why reasoning is unavailable for this response"
                    }
                    onClick={() => onToggleThinking(message.id)}
                  >
                    {thinkingOpen ? "Hide thinking" : "View thinking"}
                  </button>
                </>
              )}
            </div>
            {!isUser && thinkingOpen && (
              <div className="thinking-panel">
                {hasThinking ? (
                  message.thinking
                ) : (
                  <>
                    <strong>Reasoning unavailable for this response.</strong>
                    <p>
                      The selected model returned an answer but did not send a reasoning trace.
                      Neo does not invent one. Choose a reasoning-capable model to view model-provided
                      reasoning when it is available.
                    </p>
                  </>
                )}
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
          {hasThinking ? generation.thinking : (generation?.statusDetail || "Waiting for response...")}
        </div>
        {hasContent && (
          // Rendered while streaming too, so a code block does not appear only once the
          // closing fence arrives. Escaped by renderMessageHtml.
          <div
            className="chat-content live-answer"
            dangerouslySetInnerHTML={{ __html: renderMessageHtml(generation.content) }}
          />
        )}
      </div>
    </article>
  );
}

function formatFileSize(value) {
  if (!Number.isFinite(value)) return "";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
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
  projects,
  selectedProjectId,
  onProjectChange,
  agentDefinitions,
  selectedAgentDefinitionId,
  onAgentDefinitionChange,
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
  onSaveAgentRun,
  onRefreshAgentRun,
  attachments = [],
  onAttachFiles,
  onRemoveAttachment,
  attaching = false,
  attachError = "",
  generating = false,
  onStop,
  stopping = false,
}) {
  const textareaRef = useRef(null);
  const attachInputRef = useRef(null);
  const [showCodingWorkbench, setShowCodingWorkbench] = useState(false);

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
              className={mode === "chatbot" ? "active" : ""} onClick={() => onModeChange("chatbot")}>Chat</button>
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
                  disabled={disabled} aria-label="Select optional project for agent">
                  <option value="">Optional project</option>
                  {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
                </select>
              </label>
              <label className="agent-task-picker">
                <span>Agent</span>
                <select value={selectedAgentDefinitionId} onChange={(event) => onAgentDefinitionChange(event.target.value)}
                  disabled={disabled} aria-label="Select agent definition">
                  <option value="general">General</option>
                  {agentDefinitions.map((agent) => <option key={agent.id} value={agent.id}>{agent.display_name || agent.name}</option>)}
                </select>
              </label>
            </div>
          )}
        </div>
        {mode === "chatbot" && attachments.length > 0 ? (
          <div className="chat-attachments">
            {attachments.map((file) => (
              <span className="chat-attachment" key={file.id}>
                <span className="chat-attachment-name">
                  {file.metadata?.relative_path || file.display_name}
                </span>
                <small>{formatFileSize(file.size_bytes)}</small>
                <button
                  type="button"
                  onClick={() => onRemoveAttachment?.(file.id)}
                  aria-label={`Remove ${file.display_name}`}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        ) : null}
        {mode === "chatbot" && attachError ? (
          <div className="chat-attach-error">{attachError}</div>
        ) : null}
        <form className="chat-input-form" onSubmit={onSubmit}>
          {mode === "chatbot" ? (
            <>
              <input
                ref={attachInputRef}
                type="file"
                multiple
                hidden
                onChange={(event) => {
                  const picked = Array.from(event.target.files || []);
                  event.target.value = "";
                  if (picked.length) onAttachFiles?.(picked);
                }}
              />
              <button
                type="button"
                className="chat-attach-button"
                onClick={() => attachInputRef.current?.click()}
                disabled={disabled || attaching}
                title="Attach files"
                aria-label="Attach files"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                  strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M21 11.5 12.5 20a5 5 0 0 1-7-7l8-8a3.5 3.5 0 0 1 5 5l-8 8a2 2 0 0 1-3-3l7.5-7.5" />
                </svg>
              </button>
            </>
          ) : null}
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
                disabled={disabled || planningTasks || !value.trim()}>Plan</button>
              <NeoButton type="submit" className="agent-run-button"
                disabled={disabled || !value.trim()}
                aria-label="Start Agent" title="Start Agent">Start</NeoButton>
            </div>
          ) : (
            generating ? (
              <button
                type="button"
                className="send-button stop-button"
                onClick={onStop}
                disabled={stopping}
                aria-label="Stop generating"
                title="Stop generating"
              >
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <rect x="7" y="7" width="10" height="10" rx="1.5" />
                </svg>
              </button>
            ) : (
              <NeoButton type="submit" className="send-button" disabled={disabled || !value.trim()}
                aria-label="Send message" title="Send message">{"\u2191"}</NeoButton>
            )
          )}
        </form>
        {mode === "agent" && !value.trim() ? (
          <div className="agent-mode-hint">Give Neo an objective or paste a plan. It will create and complete its own checklist.</div>
        ) : null}
        {mode === "agent" && agentMessage ? <div className="agent-mode-message">{agentMessage}</div> : null}
        {mode === "agent" ? (
          <button
            className="agent-workbench-trigger"
            type="button"
            onClick={() => setShowCodingWorkbench(true)}
          >
            <span>ADVANCED WORKBENCH</span>
            Plan, patch, test, and checkpoint a coding change
            <small>explicit approvals remain required</small>
          </button>
        ) : null}
        {mode === "agent" && proposedPlan ? (
          <div className="agent-plan-preview">
            <div className="agent-plan-preview-head">
              <div><strong>{proposedPlan.parent_task.title}</strong><span>{proposedPlan.subtasks.length} checklist items</span></div>
              <button type="button" onClick={onCancelPlan}>Cancel</button>
            </div>
            <ol>{proposedPlan.subtasks.map((task) => <li key={task.order}><strong>{task.title}</strong><span>{task.description}</span></li>)}</ol>
            <div className="agent-plan-actions">
              <button type="button" onClick={onCreatePlannedTasks} disabled={disabled}>Save checklist</button>
              <button type="button" onClick={onCreatePlannedTasksAndRun} disabled={disabled}>Start with checklist</button>
            </div>
          </div>
        ) : null}
        {mode === "agent" && createdTasks?.length ? (
          <div className="agent-created-tasks">
            <strong>Checklist created</strong>
            <span>{createdTasks[0].title} with {Math.max(0, createdTasks.length - 1)} steps.</span>
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
              <button type="button" onClick={onToggleAgentDetails}>{agentDetailsOpen ? "Hide Run" : "Open Run"}</button>
              {agentRun.run.status === "completed" ? (
                <button type="button" onClick={onSaveAgentRun} disabled={disabled}>Save Output to Note</button>
              ) : null}
            </div>
            {agentDetailsOpen ? (
              <div className="chat-agent-details">
                {agentRun.run.agent_definition_snapshot ? <div className="agent-run-card"><strong>Active agent: {agentRun.run.agent_definition_snapshot.display_name || agentRun.run.agent_definition_snapshot.name}</strong><p className="task-help">Agents cannot bypass approvals; any protected actions still require explicit approval.</p></div> : null}
                {agentRun.steps.map((step) => (
                  <div key={step.id}><span>{step.title}</span><span>{formatAgentStatus(step.status)}</span></div>
                ))}
                {agentRun.tool_calls?.length ? <details className="agent-run-card"><summary><strong>Tool calls</strong></summary><ol>{agentRun.tool_calls.map((call) => <li key={call.id}><strong>{call.tool_id}</strong> · {formatAgentStatus(call.status)} · approval {call.approval_status}{call.error ? ` · ${call.error}` : ""}</li>)}</ol></details> : null}
                {agentRun.run.error ? <div className="chat-agent-error">{agentRun.run.error}</div> : null}
                {agentRun.run.final_output ? <pre>{agentRun.run.final_output}</pre> : null}
                <RecoveryPanel
                  runType="agent"
                  runId={agentRun.run.id}
                  onUpdated={onRefreshAgentRun}
                />
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
      <div className="chat-input-disclaimer">
        {mode === "agent"
          ? "Agent runs are task-linked and audited. No chat message is sent in Agent mode."
          : "Neo is an AI and it can make mistakes. Please double-check responses."}
      </div>
      {mode === "agent" && showCodingWorkbench ? (
        <Modal
          title="Advanced Coding Workbench"
          onClose={() => setShowCodingWorkbench(false)}
          wide
          className="coding-workbench-modal"
        >
          <CodingAgent initialProjectId={selectedProjectId} />
        </Modal>
      ) : null}
    </div>
  );
}

function WebSearchSettingsDialog({ onClose }) {
  const [searchConfig, setSearchConfig] = useState(null);
  const [provider, setProvider] = useState("duckduckgo");
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
        setProvider(config.provider || "duckduckgo");
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
      setProvider(config.provider || "duckduckgo");
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
                <option value="duckduckgo">DuckDuckGo</option>
                <option value="bing_html">Bing</option>
                <option value="searxng">SearXNG</option>
                <option value="tavily">Tavily</option>
                <option value="disabled">Disabled</option>
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

const EMPTY_PROVIDER_FORM = {
  id: "",
  name: "",
  provider_type: "ollama",
  base_url: "http://127.0.0.1:11434",
  api_key_ref: "",
  default_model: "",
  enabled: true,
  priority: 100,
  timeout_seconds: 60,
};

const EMPTY_MODEL_FORM = {
  id: "",
  provider_id: "",
  model_name: "",
  display_name: "",
  context_window: "",
  max_output_tokens: 512,
  supports_tools: false,
  supports_json: false,
  supports_vision: false,
  supports_embeddings: false,
  enabled: true,
};

function LLMSettingsDialog({ onClose, onChanged }) {
  const [registry, setRegistry] = useState({ providers: [], models: [], routes: [], calls: [] });
  const [providerForm, setProviderForm] = useState(EMPTY_PROVIDER_FORM);
  const [modelForm, setModelForm] = useState(EMPTY_MODEL_FORM);
  const [editingProviderId, setEditingProviderId] = useState(null);
  const [editingModelId, setEditingModelId] = useState(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState(null);
  const [discoveringId, setDiscoveringId] = useState(null);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");

  const load = useCallback(async () => {
    const [providers, models, routes, usage, legacy] = await Promise.all([
      api.llmProviders(), api.llmModels(), api.llmRoutes(), api.llmUsage(), api.llms(),
    ]);
    const next = {
      providers: providers.providers || [], models: models.models || [],
      routes: routes.routes || [], calls: usage.calls || [],
    };
    setRegistry(next);
    onChanged(legacy);
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

  function updateProviderField(key, value) {
    setProviderForm((current) => {
      const next = { ...current, [key]: value };
      if (key === "provider_type" && !editingProviderId) {
        next.base_url = value === "ollama" ? "http://127.0.0.1:11434" : "";
      }
      return next;
    });
  }

  function resetProviderForm() {
    setEditingProviderId(null);
    setProviderForm(EMPTY_PROVIDER_FORM);
  }

  function editProvider(provider) {
    setEditingProviderId(provider.id);
    setProviderForm({ ...EMPTY_PROVIDER_FORM, ...provider, api_key_ref: provider.api_key_ref || "" });
    setStatus("");
    setError("");
  }

  async function saveProvider(event) {
    event.preventDefault();
    setSaving(true);
    setError("");
    setStatus("");
    try {
      const payload = {
        ...providerForm,
        id: providerForm.id.trim() || null,
        name: providerForm.name.trim(),
        base_url: providerForm.base_url.trim() || null,
        api_key_ref: providerForm.api_key_ref.trim() || null,
        default_model: providerForm.default_model.trim() || null,
        priority: Number(providerForm.priority),
        timeout_seconds: Number(providerForm.timeout_seconds),
      };
      if (editingProviderId) {
        delete payload.id;
        await api.updateLlmProvider(editingProviderId, payload);
      } else {
        await api.createLlmProvider(payload);
      }
      await load();
      setStatus(editingProviderId ? "Provider updated." : "Provider added.");
      resetProviderForm();
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setSaving(false);
    }
  }

  function editModel(model) {
    setEditingModelId(model.id);
    setModelForm({
      ...EMPTY_MODEL_FORM, ...model,
      context_window: model.context_window || "",
      max_output_tokens: model.max_output_tokens || 512,
    });
  }

  async function saveModel(event) {
    event.preventDefault();
    setSaving(true); setError(""); setStatus("");
    try {
      const payload = {
        ...modelForm,
        id: modelForm.id.trim() || null,
        model_name: modelForm.model_name.trim(),
        display_name: modelForm.display_name.trim() || null,
        context_window: modelForm.context_window ? Number(modelForm.context_window) : null,
        max_output_tokens: Number(modelForm.max_output_tokens),
      };
      if (editingModelId) {
        delete payload.id; delete payload.provider_id;
        await api.updateLlmModel(editingModelId, payload);
      } else {
        await api.createLlmModel(payload);
      }
      await load();
      setStatus(editingModelId ? "Model updated." : "Model added.");
      setEditingModelId(null); setModelForm(EMPTY_MODEL_FORM);
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setSaving(false);
    }
  }

  async function discoverModels(provider) {
    setDiscoveringId(provider.id);
    setError("");
    setStatus("");
    try {
      const result = await api.discoverLlmModels(provider.id);
      await load();
      const names = (result.added || []).map((model) => model.model_name);
      setStatus(
        names.length
          ? `Registered ${names.length} model${names.length === 1 ? "" : "s"}: ${names.join(", ")}.`
          : "No new models. Everything this provider serves is already registered.",
      );
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setDiscoveringId(null);
    }
  }

  async function testProvider(provider) {
    const model = registry.models.find((item) => item.provider_id === provider.id && item.enabled);
    if (!model) { setError("Add an enabled model before testing this provider."); return; }
    setTestingId(provider.id); setError(""); setStatus("");
    try {
      const result = await api.testLlmProvider(provider.id, model.id);
      setStatus(result.available ? `Health passed in ${result.latency_ms} ms.` : result.error);
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setTestingId(null);
    }
  }

  async function updateRoute(route, modelId, fallback = false) {
    const model = registry.models.find((item) => item.id === modelId);
    try {
      const payload = fallback
        ? { fallback_provider_id: model?.provider_id || null, fallback_model_id: model?.id || null }
        : { provider_id: model?.provider_id || null, model_id: model?.id || null };
      await api.updateLlmRoute(route.route_name, payload);
      await load(); setStatus(`${route.route_name} route updated.`);
    } catch (nextError) {
      setError(errorMessage(nextError));
    }
  }

  async function toggleProvider(provider) {
    setError("");
    try {
      await api.updateLlmProvider(provider.id, { enabled: !provider.enabled });
      await load();
      setStatus(`${provider.name} ${provider.enabled ? "disabled" : "enabled"}.`);
    } catch (nextError) {
      setError(errorMessage(nextError));
    }
  }

  async function testRoute(routeName) {
    setTestingId(routeName); setError("");
    try {
      const result = await api.testLlmRoute(routeName);
      setStatus(result.available ? `${routeName} is available.` : result.error);
    } catch (nextError) {
      setError(errorMessage(nextError));
    } finally {
      setTestingId(null);
    }
  }

  return (
    <Modal title="LLM Providers" onClose={onClose} wide className="llm-settings-dialog">
      <p className="dialog-caption">API keys are read from environment variables only. Fallbacks and failures are recorded in usage history.</p>
      <div className="llm-settings-layout">
        <section className="llm-config-list">
          <div className="llm-section-heading">Providers and models</div>
          {loading ? (
            <p className="dialog-caption">Loading...</p>
          ) : registry.providers.length === 0 ? (
            <p className="dialog-caption">No providers configured.</p>
          ) : (
            registry.providers.map((provider) => (
              <article className="llm-config-card" key={provider.id}>
                <div className="llm-config-title">
                  <strong>{provider.name}</strong><span>{provider.enabled ? "Enabled" : "Disabled"}</span>
                </div>
                <div className="llm-config-meta">{provider.provider_type} · {provider.base_url || "No endpoint"}</div>
                <div className="llm-config-meta">Key: {provider.api_key_ref || "not required"} {provider.api_key_configured ? "(available)" : ""}</div>
                {registry.models.filter((model) => model.provider_id === provider.id).map((model) => (
                  <button type="button" className="llm-model-row" key={model.id} onClick={() => editModel(model)}>
                    {model.display_name || model.model_name} · {model.enabled ? "enabled" : "disabled"}
                  </button>
                ))}
                <div className="llm-card-actions">
                  <NeoButton onClick={() => testProvider(provider)} disabled={testingId === provider.id}>
                    {testingId === provider.id ? "Testing..." : "Health"}
                  </NeoButton>
                  {provider.provider_type === "ollama" && (
                    <NeoButton
                      onClick={() => discoverModels(provider)}
                      disabled={discoveringId === provider.id}
                      title="Register models this provider already serves"
                    >
                      {discoveringId === provider.id ? "Syncing..." : "Sync models"}
                    </NeoButton>
                  )}
                  <NeoButton onClick={() => editProvider(provider)}>Edit</NeoButton>
                  <NeoButton onClick={() => toggleProvider(provider)}>
                    {provider.enabled ? "Disable" : "Enable"}
                  </NeoButton>
                </div>
              </article>
            ))
          )}
        </section>

        <form className="llm-config-form" onSubmit={saveProvider}>
          <div className="llm-section-heading">{editingProviderId ? "Edit provider" : "Add provider"}</div>
          <Field label="Connection type">
            <select value={providerForm.provider_type} onChange={(event) => updateProviderField("provider_type", event.target.value)}>
              <option value="ollama">Local / Ollama</option>
              <option value="openai_compatible">OpenAI-compatible / API or local</option>
              <option value="disabled">Disabled</option>
            </select>
          </Field>
          <Field label="Provider ID">
            <input value={providerForm.id} onChange={(event) => updateProviderField("id", event.target.value)} placeholder="my-provider" disabled={Boolean(editingProviderId)} />
          </Field>
          <Field label="Display name">
            <input value={providerForm.name} onChange={(event) => updateProviderField("name", event.target.value)} required />
          </Field>
          <Field label="Endpoint">
            <input value={providerForm.base_url} onChange={(event) => updateProviderField("base_url", event.target.value)} placeholder="https://provider.example/v1" />
          </Field>
          <Field label="API key environment variable">
            <input value={providerForm.api_key_ref} onChange={(event) => updateProviderField("api_key_ref", event.target.value)} placeholder="OPENAI_API_KEY" />
          </Field>
          <Field label="Default model name">
            <input value={providerForm.default_model} onChange={(event) => updateProviderField("default_model", event.target.value)} />
          </Field>
          <div className="llm-number-fields">
            <Field label="Timeout (seconds)">
              <input type="number" min="1" max="3600" value={providerForm.timeout_seconds} onChange={(event) => updateProviderField("timeout_seconds", event.target.value)} />
            </Field>
            <Field label="Priority">
              <input type="number" min="0" value={providerForm.priority} onChange={(event) => updateProviderField("priority", event.target.value)} />
            </Field>
          </div>
          <label className="llm-enabled-toggle">
            <input type="checkbox" checked={providerForm.enabled} onChange={(event) => updateProviderField("enabled", event.target.checked)} />
            Enabled
          </label>
          <div className="settings-actions">
            <NeoButton type="submit" disabled={saving}>{saving ? "Saving..." : editingProviderId ? "Save provider" : "Add provider"}</NeoButton>
            {editingProviderId && <NeoButton type="button" onClick={resetProviderForm}>Cancel</NeoButton>}
          </div>
        </form>
      </div>

      <div className="llm-settings-layout llm-registry-secondary">
        <form className="llm-config-form" onSubmit={saveModel}>
          <div className="llm-section-heading">{editingModelId ? "Edit model" : "Add model"}</div>
          <Field label="Provider"><select value={modelForm.provider_id} disabled={Boolean(editingModelId)} onChange={(event) => setModelForm({ ...modelForm, provider_id: event.target.value })}><option value="">Select provider</option>{registry.providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.name}</option>)}</select></Field>
          <Field label="Model ID"><input value={modelForm.id} disabled={Boolean(editingModelId)} onChange={(event) => setModelForm({ ...modelForm, id: event.target.value })} placeholder="optional-stable-id" /></Field>
          <Field label="Model name"><input value={modelForm.model_name} onChange={(event) => setModelForm({ ...modelForm, model_name: event.target.value })} required /></Field>
          <Field label="Display name"><input value={modelForm.display_name} onChange={(event) => setModelForm({ ...modelForm, display_name: event.target.value })} /></Field>
          <Field label="Max output tokens"><input type="number" min="1" value={modelForm.max_output_tokens} onChange={(event) => setModelForm({ ...modelForm, max_output_tokens: event.target.value })} /></Field>
          <div className="llm-capabilities"><label><input type="checkbox" checked={modelForm.supports_tools} onChange={(event) => setModelForm({ ...modelForm, supports_tools: event.target.checked })} /> Tools</label><label><input type="checkbox" checked={modelForm.supports_json} onChange={(event) => setModelForm({ ...modelForm, supports_json: event.target.checked })} /> JSON</label><label><input type="checkbox" checked={modelForm.supports_vision} onChange={(event) => setModelForm({ ...modelForm, supports_vision: event.target.checked })} /> Vision</label><label><input type="checkbox" checked={modelForm.supports_embeddings} onChange={(event) => setModelForm({ ...modelForm, supports_embeddings: event.target.checked })} /> Embeddings</label></div>
          <label className="llm-enabled-toggle"><input type="checkbox" checked={modelForm.enabled} onChange={(event) => setModelForm({ ...modelForm, enabled: event.target.checked })} /> Enabled</label>
          <div className="settings-actions"><NeoButton type="submit" disabled={saving || !modelForm.provider_id}>{editingModelId ? "Save model" : "Add model"}</NeoButton>{editingModelId && <NeoButton type="button" onClick={() => { setEditingModelId(null); setModelForm(EMPTY_MODEL_FORM); }}>Cancel</NeoButton>}</div>
        </form>
        <section className="llm-config-list">
          <div className="llm-section-heading">Role routes and fallbacks</div>
          {registry.routes.map((route) => <div className="llm-route-row" key={route.id}><strong>{route.route_name}</strong><select aria-label={`${route.route_name} primary model`} value={route.model_id || ""} onChange={(event) => updateRoute(route, event.target.value)}>{registry.models.filter((model) => model.enabled).map((model) => <option key={model.id} value={model.id}>{model.display_name || model.model_name}</option>)}</select><select aria-label={`${route.route_name} fallback model`} value={route.fallback_model_id || ""} onChange={(event) => updateRoute(route, event.target.value, true)}><option value="">No fallback</option>{registry.models.filter((model) => model.enabled && model.id !== route.model_id).map((model) => <option key={model.id} value={model.id}>{model.display_name || model.model_name}</option>)}</select><NeoButton onClick={() => testRoute(route.route_name)}>{testingId === route.route_name ? "Testing..." : "Test"}</NeoButton></div>)}
        </section>
      </div>
      <section className="llm-usage-section"><div className="llm-section-heading">Usage history</div>{registry.calls.length === 0 ? <p className="dialog-caption">No routed calls recorded yet.</p> : <div className="llm-usage-list">{registry.calls.slice(0, 20).map((call) => <div key={call.id}><strong>{call.route_name}</strong> · {call.status} · {call.total_tokens ?? "—"} tokens · {call.latency_ms ?? "—"} ms{call.fallback_used ? " · fallback" : ""}{call.error ? ` · ${call.error}` : ""}</div>)}</div>}</section>
      {error && <div className="neo-error">{error}</div>}
      {status && <div className="settings-status">{status}</div>}
    </Modal>
  );
}

function SettingsDialog({ onOpenAccount, onOpenAgentic, onOpenLLMs, onOpenProviderRuntime, onOpenEvaluationHarness, onOpenWorkspaceOrchestration, onOpenContinuity, onOpenRules, onOpenAgents, onOpenTools, onOpenBundles, onOpenGitHub, onOpenContextMemory, onOpenMemoryRetrieval, onOpenReliableWebSearch, onOpenCommandSandbox, onOpenLsp, onOpenMemory, onOpenNotes, onOpenProjects, onOpenResearch, onOpenTasks, onOpenWebSearch, onClose }) {
  const groups = [
    {
      title: "Intelligence",
      icon: "memory",
      description: "Models, behavior, and agent configuration.",
      items: [
        ["Agentic Runs", "Plan, execute, verify, and reflect", onOpenAgentic],
        ["LLM Providers", "Models, routes, fallbacks, and usage", onOpenLLMs],
        ["Provider Runtime", "Health, rate limits, streaming, and request audit", onOpenProviderRuntime],
        ["Evaluation Harness", "Offline scoring, reports, baselines, and safety regression", onOpenEvaluationHarness],
        ["Workspace Orchestration", "Plans, evidence, readiness, risks, and project delivery", onOpenWorkspaceOrchestration],
        ["Continuity", "Redacted exports, import validation, and resumable state", onOpenContinuity],
        ["Rules & Profiles", "Scoped guidance and resolution priority", onOpenRules],
        ["Agents", "Roles, permissions, tools, and skills", onOpenAgents],
      ],
    },
    {
      title: "Capabilities",
      icon: "terminal",
      description: "Connected tools and runtime services.",
      items: [
        ["Tools & Skills", "Tool servers, definitions, and approvals", onOpenTools],
        ["Web Search", "Search provider and availability", onOpenWebSearch],
        ["Reliable Web Search", "Evidence, citations, conflicts, and audit", onOpenReliableWebSearch],
        ["Language Server", "Workspace language intelligence", onOpenLsp],
        ["Command Sandbox", "Controlled command policy and history", onOpenCommandSandbox],
      ],
    },
    {
      title: "Knowledge",
      icon: "layers",
      description: "Stored context and research materials.",
      items: [
        ["Memory", "Durable personal context", onOpenMemory],
        ["Context Memory", "Long-run summaries and compaction", onOpenContextMemory],
        ["Workspace Retrieval", "Searchable workspace history, scoring, and audit", onOpenMemoryRetrieval],
        ["Research", "Sources and research sessions", onOpenResearch],
        ["Notes", "Saved working notes", onOpenNotes],
      ],
    },
    {
      title: "Account",
      icon: "shield",
      description: "Your name, picture, password, and session.",
      items: [
        ["Account", "Name, profile picture, and password", onOpenAccount],
      ],
    },
    {
      title: "Workspace",
      icon: "folder",
      description: "Projects, work tracking, and portability.",
      items: [
        ["Projects", "Organize related chats and work", onOpenProjects],
        ["Bundles", "Export and import sanitized archives", onOpenBundles],
        ["GitHub", "Issue and pull request workflow", onOpenGitHub],
      ],
    },
  ];

  return (
    <Modal title="Settings" onClose={onClose} className="settings-dialog">
      <div className="set-intro">
        <h3>Control center</h3>
        <p>Configure Neo without leaving your workspace.</p>
      </div>
      <div className="set-grid" aria-label="Settings categories">
        {groups.map((group) => (
          <section className="set-group" key={group.title}>
            <div className="set-group-head">
              <WorkspaceIcon name={group.icon} />
              <h4>{group.title}</h4>
            </div>
            <div className="set-links">
              {group.items.map(([title, description, onClick]) => (
                <button className="set-link" type="button" onClick={onClick} key={title}>
                  <span className="set-link-text">
                    <strong>{title}</strong>
                    <small>{description}</small>
                  </span>
                  <span className="set-link-arrow" aria-hidden="true">→</span>
                </button>
              ))}
            </div>
          </section>
        ))}
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

function NeoApp({ profile, onProfileUpdated, onSwitchProfile }) {
  const [sidebar, setSidebar] = useState(EMPTY_SIDEBAR);
  const [activeChat, setActiveChat] = useState(null);
  const [messages, setMessages] = useState([]);
  const [selectedProjectId, setSelectedProjectId] = useState(null);
  const [showNewProjectForm, setShowNewProjectForm] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showAccount, setShowAccount] = useState(false);
  const [chatAttachments, setChatAttachments] = useState([]);
  const [attachingFiles, setAttachingFiles] = useState(false);
  const [attachError, setAttachError] = useState("");
  const [showLlmSettings, setShowLlmSettings] = useState(false);
  const [showProviderRuntime, setShowProviderRuntime] = useState(false);
  const [showEvaluationHarness, setShowEvaluationHarness] = useState(false);
  const [showWorkspaceOrchestration, setShowWorkspaceOrchestration] = useState(false);
  const [showContinuity, setShowContinuity] = useState(false);
  const [showWebSearchSettings, setShowWebSearchSettings] = useState(false);
  const [showRulesSettings, setShowRulesSettings] = useState(false);
  const [showAgentSettings, setShowAgentSettings] = useState(false);
  const [showToolsSettings, setShowToolsSettings] = useState(false);
  const [showBundles, setShowBundles] = useState(false);
  const [showGitHub, setShowGitHub] = useState(false);
  const [showContextMemory, setShowContextMemory] = useState(false);
  const [showMemoryRetrieval, setShowMemoryRetrieval] = useState(false);
  const [showCommandSandbox, setShowCommandSandbox] = useState(false);
  const [showLsp, setShowLsp] = useState(false);
  const [showAgentic, setShowAgentic] = useState(false);
  const [showReliableWebSearch, setShowReliableWebSearch] = useState(false);
  const [showMemory, setShowMemory] = useState(false);
  const [memoryEnabled, setMemoryEnabled] = useState(true);
  const [memoryIncognito, setMemoryIncognito] = useState(false);
  const [pendingDelete, setPendingDelete] = useState(null);
  const [composerValue, setComposerValue] = useState("");
  const [editingMessageId, setEditingMessageId] = useState(null);
  const [editingValue, setEditingValue] = useState("");
  const [openThinkingMessageId, setOpenThinkingMessageId] = useState(null);
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [streamingAssistant, setStreamingAssistant] = useState(null);
  const [generationChatId, setGenerationChatId] = useState(null);
  const [activeGenerationId, setActiveGenerationId] = useState(null);
  const [generationStartedAt, setGenerationStartedAt] = useState(null);
  const [elapsedMs, setElapsedMs] = useState(0);
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
  const [agentDefinitions, setAgentDefinitions] = useState([]);
  const [selectedAgentDefinitionId, setSelectedAgentDefinitionId] = useState("general");
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
  const createChatPromiseRef = useRef(null);
  const visibleChatIdRef = useRef(null);

  const refreshSidebar = useCallback(async () => {
    const nextSidebar = await api.sidebar();
    setSidebar(nextSidebar);
    return nextSidebar;
  }, []);

  const loadAgentContext = useCallback(async () => {
    setAgentTasksLoading(true);
    try {
      const [taskData, projectData, agentData] = await Promise.all([
        api.tasksList({ includeArchived: false, pinnedFirst: true, limit: 100 }),
        api.projectsList({ includeArchived: false, pinnedFirst: true, limit: 100 }),
        api.agentDefinitions(false),
      ]);
      setAgentTasks(taskData.tasks || []);
      setAgentProjects(projectData.projects || []);
      setAgentDefinitions(agentData.definitions || []);
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
      updatePermalink(chatPermalink(thread.chat.id, thread.chat.project_id), { replace: options.history === "replace" });
    }
    const generation = await api.activeChatGeneration(thread.chat.id).catch(() => null);
    if (generation) {
      setActiveGenerationId(generation.id);
      setGenerationChatId(thread.chat.id);
      setSending(true);
      setGenerationStartedAt(parseNeoTimestamp(generation.started_at || generation.created_at) || Date.now());
      setStreamingAssistant({
        rawContent: generation.partial_response || "",
        ...splitGeneratedText(generation.partial_response || ""),
        thinking: generation.thinking || splitGeneratedText(generation.partial_response || "").thinking,
        statusDetail: generation.status_detail || "",
      });
    } else {
      setActiveGenerationId(null);
    }
    return thread;
  }, []);

  const createActiveChat = useCallback(
    async (projectId = null, options = {}) => {
      if (createChatPromiseRef.current) {
        return createChatPromiseRef.current;
      }
      const creation = (async () => {
        const chat = await api.createChat(projectId);
        setActiveChat(chat);
        if (options.resetMessages !== false) {
          setMessages([]);
        }
        setSelectedProjectId(chat.project_id);
        localStorage.setItem("neo-active-chat-id", String(chat.id));
        updatePermalink(chatPermalink(chat.id, chat.project_id), { replace: options.history === "replace" });
        await refreshSidebar();
        return chat;
      })();
      createChatPromiseRef.current = creation;
      try {
        return await creation;
      } finally {
        if (createChatPromiseRef.current === creation) {
          createChatPromiseRef.current = null;
        }
      }
    },
    [refreshSidebar],
  );

  useEffect(() => {
    if (!activeGenerationId || !activeChat?.id) {
      return undefined;
    }
    let cancelled = false;
    let timerId = null;

    async function pollGeneration() {
      try {
        const generation = await api.chatGeneration(activeChat.id, activeGenerationId);
        if (cancelled) return;
        const rawContent = generation.partial_response || "";
        const parsed = splitGeneratedText(rawContent);
        setStreamingAssistant({
          rawContent,
          ...parsed,
          thinking: generation.thinking || parsed.thinking,
          statusDetail: generation.status_detail || "",
        });
        if (generation.status === "completed") {
          await loadChat(activeChat.id, { history: "none" });
          if (!cancelled) {
            setActiveGenerationId(null);
            setSending(false);
            setStopping(false);
            setGenerationStartedAt(null);
            setGenerationChatId(null);
            setStreamingAssistant(null);
            await refreshSidebar();
          }
          return;
        }
        if (generation.status === "cancelled") {
          await loadChat(activeChat.id, { history: "none" });
          if (!cancelled) {
            setActiveGenerationId(null);
            setSending(false);
            setStopping(false);
            setGenerationStartedAt(null);
            setGenerationChatId(null);
            setStreamingAssistant(null);
            await refreshSidebar();
          }
          return;
        }
        if (generation.status === "failed") {
          setStatusError(generation.error || "Neo could not finish this response.");
          await loadChat(activeChat.id, { history: "none" });
          if (!cancelled) {
            setActiveGenerationId(null);
            setSending(false);
            setGenerationStartedAt(null);
            setGenerationChatId(null);
            setStreamingAssistant(null);
            await refreshSidebar();
          }
          return;
        }
        timerId = window.setTimeout(pollGeneration, 250);
      } catch (error) {
        if (!cancelled) {
          setStatusError(`Could not check the response: ${errorMessage(error)}`);
          timerId = window.setTimeout(pollGeneration, 1000);
        }
      }
    }

    pollGeneration();
    return () => {
      cancelled = true;
      if (timerId) window.clearTimeout(timerId);
    };
  }, [activeChat?.id, activeGenerationId, loadChat, refreshSidebar]);

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

        if (permalink?.type === "chat" || permalink?.type === "projectChat") {
          try {
            await loadChat(permalink.id, { history: "replace" });
          } catch {
            await createActiveChat(null, { history: "replace" });
          }
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
      if (permalink?.type === "chat" || permalink?.type === "projectChat") {
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
      updatePermalink(chatPermalink(activeChat.id, activeChat.project_id));
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

  async function handleRenameChat(chat, title) {
    setStatusError("");
    try {
      const updated = await api.renameChat(chat.id, title);
      // The header reads from activeChat, so the open conversation retitles immediately
      // rather than waiting for the next sidebar load.
      setActiveChat((current) => (current?.id === chat.id ? { ...current, ...updated } : current));
      await refreshSidebar();
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
        await api.deleteChat(pendingDelete.id, { memoryEnabled, memoryIncognito });
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
      const result = await api.rerunChatMessage(
        activeChat.id,
        message.id,
        cleaned,
        selectedLlmId || null,
        clientRequestId(),
        { ...browserChatContext(), memoryEnabled, memoryIncognito },
      );
      setMessages((current) => {
        const index = current.findIndex((item) => item.id === message.id);
        if (index === -1) return current;
        return current.slice(0, index + 1).map((item) =>
          item.id === message.id ? { ...item, content: cleaned } : item,
        );
      });
      setEditingMessageId(null);
      setEditingValue("");
      setSending(true);
      setGenerationChatId(activeChat.id);
      setActiveGenerationId(result.generation.id);
      setGenerationStartedAt(parseNeoTimestamp(result.generation.created_at) || Date.now());
      setElapsedMs(0);
      setStreamingAssistant({ rawContent: "", content: "", thinking: "" });
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
      setGenerationChatId(chat.id);
      const result = await api.startChatGeneration(
        chat.id,
        prompt,
        selectedLlmId || null,
        clientRequestId(),
        { ...browserChatContext(), memoryEnabled, memoryIncognito },
      );
      setActiveGenerationId(result.generation.id);
    } catch (error) {
      setMessages((current) =>
        current.map((message) =>
          message.id === pendingId ? { ...message, failed: true } : message,
        ),
      );
      setComposerValue(prompt);
      setStatusError(`${errorMessage(error)}. Your message was not sent, but it was kept.`);
      setSending(false);
      setGenerationStartedAt(null);
      setGenerationChatId(null);
      setStreamingAssistant(null);
    }
  }

  async function handleAttachFiles(files) {
    setAttachingFiles(true);
    setAttachError("");
    try {
      const uploaded = [];
      for (const file of files) {
        const data = await api.uploadFile(file);
        uploaded.push(data.file);
      }
      setChatAttachments((current) => [...current, ...uploaded]);
    } catch (error) {
      setAttachError(errorMessage(error));
    } finally {
      setAttachingFiles(false);
    }
  }

  function handleRemoveAttachment(fileId) {
    setChatAttachments((current) => current.filter((file) => file.id !== fileId));
  }

  /**
   * The chat API takes a single prompt string, so attached files travel as a bounded
   * context block appended to the message. The upload itself is a real workspace file,
   * so it stays available on the Files page afterwards.
   */
  function promptWithAttachments(prompt) {
    if (!chatAttachments.length) return prompt;
    const LIMIT = 4000;
    const blocks = chatAttachments.map((file) => {
      const name = file.metadata?.relative_path || file.display_name;
      const text = file.extracted_text || "";
      if (!text) return `--- Attached file: ${name} (no extractable text) ---`;
      const clipped = text.slice(0, LIMIT);
      return `--- Attached file: ${name} ---\n${clipped}${text.length > LIMIT ? "\n…(truncated)" : ""}`;
    });
    return `${prompt}\n\n${blocks.join("\n\n")}`;
  }

  async function handleStopGeneration() {
    if (!activeGenerationId || !activeChat?.id || stopping) return;
    setStopping(true);
    try {
      await api.cancelChatGeneration(activeChat.id, activeGenerationId);
      // The poll loop sees "cancelled" and tears the streaming state down.
    } catch (error) {
      setStopping(false);
      setStatusError(`Could not stop the response: ${errorMessage(error)}`);
    }
  }

  async function handleSendMessage(event) {
    event.preventDefault();
    const prompt = composerValue.trim();
    if (!prompt || sending) {
      return;
    }
    const outgoing = promptWithAttachments(prompt);
    setComposerValue("");
    setChatAttachments([]);
    setAttachError("");
    await sendPrompt(outgoing);
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
          agent_definition_id: selectedAgentDefinitionId || null,
        });
      } else {
        const result = await api.startAgentRunFromObjective({
          objective,
          project_id: selectedAgentProjectId || null,
          mode: "assist",
          auto_create_tasks: true,
          agent_definition_id: selectedAgentDefinitionId || null,
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
        agent_definition_id: selectedAgentDefinitionId || null,
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
  const activeView = showSettings ? "settings"
    : showMemory ? "memory"
      : showResearch ? "research"
        : showNotes ? "notes"
          : showProjects ? "projects"
            : showTasks ? "tasks"
              : showFiles ? "files"
                : showRepos ? "repos"
                  : "chat";
  return (
    <div className="neo-app">
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
        onRenameChat={handleRenameChat}
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
        activeView={activeView}
        profile={profile}
        onSwitchProfile={onSwitchProfile}
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
          memoryEnabled={memoryEnabled}
          memoryIncognito={memoryIncognito}
          onBack={() => setShowResearch(false)}
          onOpenNote={(noteId) => {
            setInitialNoteId(noteId);
            setShowResearch(false);
            setShowNotes(true);
          }}
        />
      ) : (
      <main className={`neo-main ${chatMode === "agent" ? "agent-chat-mode" : ""}`}>
        <header className="neo-view-header">
          <span>{activeChat?.title || "New conversation"}</span>
          <span className="neo-view-context">{chatMode === "agent" ? "AGENT MODE" : "LOCAL · PRIVATE"}</span>
        </header>
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
          generating={sending && Boolean(activeGenerationId)}
          onStop={handleStopGeneration}
          stopping={stopping}
          attachments={chatAttachments}
          onAttachFiles={handleAttachFiles}
          onRemoveAttachment={handleRemoveAttachment}
          attaching={attachingFiles}
          attachError={attachError}
          value={composerValue}
          onChange={setComposerValue}
          onSubmit={handleComposerSubmit}
          disabled={chatMode === "chatbot"
            ? sending || !activeChat?.id
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
          agentDefinitions={agentDefinitions}
          selectedAgentDefinitionId={selectedAgentDefinitionId}
          onAgentDefinitionChange={setSelectedAgentDefinitionId}
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
          onRefreshAgentRun={async () => {
            if (chatAgentRun?.run?.id) setChatAgentRun(await api.agentRun(chatAgentRun.run.id));
          }}
        />
      </main>
      )}

      {showAccount && (
        <AccountSettings
          profile={profile}
          onProfileUpdated={onProfileUpdated}
          onClose={() => setShowAccount(false)}
        />
      )}

      {showSettings && (
        <SettingsDialog
          onOpenAccount={() => { setShowSettings(false); setShowAccount(true); }}
          onOpenAgentic={() => { setShowSettings(false); setShowAgentic(true); }}
          onOpenRules={() => { setShowSettings(false); setShowRulesSettings(true); }}
          onOpenAgents={() => { setShowSettings(false); setShowAgentSettings(true); }}
          onOpenTools={() => { setShowSettings(false); setShowToolsSettings(true); }}
          onOpenBundles={() => { setShowSettings(false); setShowBundles(true); }}
          onOpenGitHub={() => { setShowSettings(false); setShowGitHub(true); }}
          onOpenContextMemory={() => { setShowSettings(false); setShowContextMemory(true); }}
          onOpenMemoryRetrieval={() => { setShowSettings(false); setShowMemoryRetrieval(true); }}
          onOpenCommandSandbox={() => { setShowSettings(false); setShowCommandSandbox(true); }}
          onOpenLsp={() => { setShowSettings(false); setShowLsp(true); }}
          onOpenLLMs={() => {
            setShowSettings(false);
            setShowLlmSettings(true);
          }}
          onOpenProviderRuntime={() => { setShowSettings(false); setShowProviderRuntime(true); }}
          onOpenEvaluationHarness={() => { setShowSettings(false); setShowEvaluationHarness(true); }}
          onOpenWorkspaceOrchestration={() => { setShowSettings(false); setShowWorkspaceOrchestration(true); }}
          onOpenContinuity={() => { setShowSettings(false); setShowContinuity(true); }}
          onOpenWebSearch={() => {
            setShowSettings(false);
            setShowWebSearchSettings(true);
          }}
          onOpenReliableWebSearch={() => { setShowSettings(false); setShowReliableWebSearch(true); }}
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
      {showProviderRuntime && <Modal title="Provider Runtime" onClose={() => setShowProviderRuntime(false)} wide><ProviderRuntime /></Modal>}
      {showEvaluationHarness && <Modal title="Evaluation Harness" onClose={() => setShowEvaluationHarness(false)} wide><EvaluationHarness /></Modal>}
      {showWorkspaceOrchestration && <Modal title="Workspace Orchestration" onClose={() => setShowWorkspaceOrchestration(false)} wide><WorkspaceOrchestration /></Modal>}
      {showContinuity && <Modal title="Continuity" onClose={() => setShowContinuity(false)} wide><Continuity /></Modal>}
      {showRulesSettings && <RulesProfiles onClose={() => setShowRulesSettings(false)} />}
      {showAgentSettings && <AgentSettings onClose={() => setShowAgentSettings(false)} />}
      {showToolsSettings && <ToolsSkillsSettings onClose={() => setShowToolsSettings(false)} />}
      {showBundles && <Modal title="Bundles" onClose={() => setShowBundles(false)} wide><Bundles /></Modal>}
      {showGitHub && <Modal title="GitHub" onClose={() => setShowGitHub(false)} wide><GitHub onClose={() => setShowGitHub(false)} /></Modal>}
      {showContextMemory && <Modal title="Context Memory" onClose={() => setShowContextMemory(false)} wide><ContextMemory /></Modal>}
      {showMemoryRetrieval && <Modal title="Workspace Retrieval" onClose={() => setShowMemoryRetrieval(false)} wide><MemoryRetrieval /></Modal>}
      {showCommandSandbox && <Modal title="Command Sandbox" onClose={() => setShowCommandSandbox(false)} wide><CommandSandbox /></Modal>}
      {showLsp && <Modal title="Language Server Protocol" onClose={() => setShowLsp(false)} wide><LspPanel /></Modal>}
      {showAgentic && <Modal title="Agentic Core" onClose={() => setShowAgentic(false)} wide><AgenticRuns /></Modal>}
      {showReliableWebSearch && <Modal title="Reliable Web Search" onClose={() => setShowReliableWebSearch(false)} wide><WebSearch /></Modal>}

      {showWebSearchSettings && (
        <WebSearchSettingsDialog onClose={() => setShowWebSearchSettings(false)} />
      )}

      {showMemory && (
        <MemoryDialog
          memoryEnabled={memoryEnabled}
          memoryIncognito={memoryIncognito}
          onMemoryEnabledChange={setMemoryEnabled}
          onMemoryIncognitoChange={setMemoryIncognito}
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

export default function App() {
  const [profile, setProfile] = useState(null);
  const [checkingSession, setCheckingSession] = useState(true);

  useEffect(() => {
    api.currentAccountProfile()
      .then((data) => setProfile(data.profile))
      .catch(() => setProfile(null))
      .finally(() => setCheckingSession(false));
  }, []);

  async function switchProfile() {
    try {
      await api.endAccountProfileSession();
    } finally {
      localStorage.removeItem("neo-active-chat-id");
      window.location.assign("/");
    }
  }

  if (checkingSession) {
    return <main className="profile-picker"><p className="profile-loading">Loading profiles…</p></main>;
  }
  if (!profile) {
    return <ProfilePicker onSignedIn={setProfile} />;
  }
  return <NeoApp profile={profile} onProfileUpdated={setProfile} onSwitchProfile={switchProfile} />;
}
