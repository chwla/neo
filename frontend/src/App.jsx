import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { api } from "./api.js";
import { createRequestId, createSendGuard } from "./sendGuard.js";
import { MessageActionsMenu } from "./MessageActionsMenu.jsx";
import AgentTurn, { TERMINAL } from "./AgentTurn.jsx";
import { useChatStream } from "./chatStream.js";
import { PaperclipIcon } from "./icons.jsx";
import { registerModal } from "./modalStack.js";
import OpenFolderDialog from "./OpenFolderDialog.jsx";
import ChatToolsPanel from "./ChatToolsPanel.jsx";
import Notes from "./Notes.jsx";
import WorkspaceIcon from "./WorkspaceIcon.jsx";
import Projects from "./Projects.jsx";
import Research from "./Research.jsx";
import Tasks from "./Tasks.jsx";
import Files from "./Files.jsx";
import Repos from "./Repos.jsx";
import RulesProfiles from "./RulesProfiles.jsx";
import AgentSettings from "./AgentSettings.jsx";
import Bundles from "./Bundles.jsx";
import GitHub from "./GitHub.jsx";
import ContextMemory from "./ContextMemory.jsx";
import CommandSandbox from "./CommandSandbox.jsx";
import LspPanel from "./LspPanel.jsx";
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
  formatMessageTime,
  formatResponseKind,
  formatTokens,
  parseNeoTimestamp,
  renderMessageHtml,
  splitGeneratedText,
} from "./chatPresentation.js";

const EMPTY_SIDEBAR = { projects: [], chats: [] };

// Short labels: the sidebar row is narrow, and "waiting_approval" in full would
// crowd out the title that identifies the chat. Only unfinished states appear --
// a chat whose run is done is an ordinary chat again, and badging it forever
// would make every thread that ever used the agent look permanently special.
const AGENT_RUN_STATUS = {
  queued: "QUEUED",
  running: "RUNNING",
  waiting_approval: "APPROVE",
};


function errorMessage(error) {
  if (!error) {
    return "";
  }
  return error.message || String(error);
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

/**
 * Escape closes any dialog -- see modalStack.js for how that is routed when dialogs
 * stack. The corner "\u00d7" used to be the only way out, which reads as a trap in the
 * taller dialogs where it scrolls away; `backLabel` adds a second, spelled-out exit
 * next to the title for the ones you can get deep inside.
 */
export function Modal({ title, children, onClose, wide = false, className = "", backLabel = "" }) {
  // Held in a ref so an inline onClose does not re-register the dialog on every
  // render, which would shuffle it back to the top of the stack.
  const closeRef = useRef(onClose);

  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => registerModal(() => closeRef.current?.()), []);

  const dialog = (
    <div className="modal-backdrop" role="presentation">
      <section
        className={`neo-dialog ${wide ? "neo-dialog-wide" : ""} ${className}`.trim()}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="dialog-title-row">
          <div className="dialog-title-main">
            {backLabel ? (
              <button className="dialog-back" onClick={onClose} type="button">
                {"\u2190"} {backLabel}
              </button>
            ) : null}
            <h2>{title}</h2>
          </div>
          <button className="dialog-close" onClick={onClose} aria-label="Close dialog (Escape)"
            title="Close (Esc)" type="button">
            {"\u00d7"}
          </button>
        </div>
        {children}
      </section>
    </div>
  );

  // Portalled to <body> rather than rendered in place. The composer that opens the
  // workbench sets backdrop-filter, and that makes it the containing block for
  // fixed-position descendants -- rendered inline, the dialog was trapped inside the
  // composer strip with its title row and close button off-screen. Going through the
  // body keeps every dialog anchored to the viewport wherever it is opened from.
  return typeof document === "undefined" ? dialog : createPortal(dialog, document.body);
}

/**
 * One chat in the sidebar, owning its own rename editing state.
 *
 * The two lists (loose chats and chats inside a project) differ only in class names,
 * so they share this row rather than growing two copies of the rename flow.
 */
/**
 * A row's own controls, behind a vertical "..." that only shows on hover or
 * focus. Same popover rules as the transcript's menu: escape, an outside click,
 * or choosing an action closes it.
 */
function RowActionsMenu({ label, className = "", children }) {
  const [open, setOpen] = useState(false);
  const menuRef = useRef(null);
  const buttonRef = useRef(null);

  useEffect(() => {
    if (!open) {
      return undefined;
    }

    function onPointerDown(event) {
      if (menuRef.current?.contains(event.target) || buttonRef.current?.contains(event.target)) {
        return;
      }
      setOpen(false);
    }

    function onKeyDown(event) {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <span className={`row-actions ${className}`.trim()}>
      <button
        ref={buttonRef}
        type="button"
        className={`row-actions-trigger${open ? " is-open" : ""}`}
        aria-expanded={open}
        aria-haspopup="true"
        aria-label={label}
        title={label}
        onClick={(event) => {
          event.preventDefault();
          setOpen((current) => !current);
        }}
      >
        {"\u22ee"}
      </button>
      <span ref={menuRef} className="row-actions-menu" hidden={!open} onClick={() => setOpen(false)}>
        {children}
      </span>
    </span>
  );
}

function SidebarChatRow({ chat, href, isActive, classes, onOpenChat, onDeleteChat, onRenameChat, onPinChat }) {
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
      <a
        className={classes.link}
        href={href}
        onClick={(event) => handlePermalinkClick(event, () => onOpenChat(chat.id))}
      >
        {chat.pinned ? <span className="chat-item-pin" aria-label="Pinned" title="Pinned">{"\u25c6"}</span> : null}
        {chat.title}
      </a>
      {/* A run in flight has to be visible from here: without the old AGENT RUNS
          section this is the only way back to work that is still happening. */}
      {AGENT_RUN_STATUS[chat.agent_status] ? (
        <span className={`agent-run-status status-${chat.agent_status}`}>
          {AGENT_RUN_STATUS[chat.agent_status]}
        </span>
      ) : null}
      <RowActionsMenu label={`Actions for ${chat.title}`} className={classes.menu}>
        <button type="button" onClick={startRename}>
          Rename
        </button>
        <button type="button" onClick={() => onPinChat?.(chat, !chat.pinned)}>
          {chat.pinned ? "Unpin chat" : "Pin chat"}
        </button>
        <button type="button" className="row-actions-danger" onClick={() => onDeleteChat(chat)}>
          Delete
        </button>
      </RowActionsMenu>
    </div>
  );
}

export function Sidebar({
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
  onPinChat,
  onDeleteProject,
  onOpenSettings,
  onOpenChatHome,
  onOpenMemory,
  onOpenResearch,
  onOpenNotes,
  onOpenTasks,
  activeView,
  profile,
  onSwitchProfile,
  collapsed = false,
  onToggleCollapsed,
}) {
  const [projectName, setProjectName] = useState("");
  const [projectsCollapsed, setProjectsCollapsed] = useState(false);
  const [chatsCollapsed, setChatsCollapsed] = useState(false);
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
  ];

  // Minimised, the sidebar keeps only what you would reopen it for: the way
  // home, a new chat, settings, and the control that brings it back.
  if (collapsed) {
    return (
      <aside className="neo-sidebar neo-sidebar-rail">
        <button className="sidebar-rail-brand" type="button" onClick={onOpenChatHome} title="Neo" aria-label="Open chat home">
          <NeoLogo />
        </button>
        <button
          className="sidebar-rail-button"
          type="button"
          onClick={onToggleCollapsed}
          aria-label="Expand sidebar"
          aria-expanded={false}
          title="Expand sidebar"
        >
          {"\u00bb"}
        </button>
        <button
          className="sidebar-rail-button"
          type="button"
          onClick={() => onNewChat(selectedProjectId)}
          aria-label="New chat"
          title="New chat"
        >
          +
        </button>
        <div className="sidebar-spacer" />
        <button
          className="sidebar-rail-button"
          type="button"
          onClick={onOpenSettings}
          aria-label="Settings"
          title="Settings"
        >
          {"\u2261"}
        </button>
      </aside>
    );
  }

  return (
    <aside className="neo-sidebar">
      <div className="sidebar-brand-row">
        <button className="sidebar-brand" type="button" onClick={onOpenChatHome} aria-label="Open chat home">
          <NeoLogo />
          <span>neo</span>
        </button>
        <button
          className="sidebar-collapse-button"
          type="button"
          onClick={onToggleCollapsed}
          aria-label="Minimise sidebar"
          aria-expanded
          title="Minimise sidebar"
        >
          {"\u00ab"}
        </button>
      </div>
      <div className="sidebar-primary-action">
        <NeoButton className="w-full" onClick={() => onNewChat(selectedProjectId)}>
          + New chat
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

      {/* The label names what the controls act on: this header owns the project list,
          and CHATS below is its own section. Collapsing lives on the label itself so the
          only button left is "+", which can no longer be confused with the toggle. */}
      <div className="sidebar-section sidebar-section-row">
        <button
          className="sidebar-section-collapse"
          type="button"
          aria-expanded={!projectsCollapsed}
          title={projectsCollapsed ? "Show projects" : "Hide projects"}
          onClick={() => setProjectsCollapsed((collapsed) => !collapsed)}
        >
          <span className="sidebar-section-caret" aria-hidden="true">
            {projectsCollapsed ? "\u25B8" : "\u25BE"}
          </span>
          PROJECTS
        </button>
        <button
          className="sidebar-section-toggle"
          type="button"
          aria-label="Create project"
          title="Create project"
          onClick={onToggleProjectForm}
        >
          +
        </button>
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
                  menu: "project-chat-menu",
                }}
                onOpenChat={onOpenChat}
                onDeleteChat={onDeleteChat}
                onRenameChat={onRenameChat}
                onPinChat={onPinChat}
              />
            ))}
          </details>
        ))
      )}

      <div className="sidebar-section sidebar-section-row">
        <button
          className="sidebar-section-collapse"
          type="button"
          aria-expanded={!chatsCollapsed}
          title={chatsCollapsed ? "Show chats" : "Hide chats"}
          onClick={() => setChatsCollapsed((collapsed) => !collapsed)}
        >
          <span className="sidebar-section-caret" aria-hidden="true">
            {chatsCollapsed ? "\u25B8" : "\u25BE"}
          </span>
          CHATS
        </button>
      </div>
      {chatsCollapsed ? null : filteredChats.length === 0 ? (
        <p className="sidebar-caption">No chats yet.</p>
      ) : (
        filteredChats.map((chat) => (
          <SidebarChatRow
            key={chat.id}
            chat={chat}
            href={chatPermalink(chat.id)}
            isActive={chat.id === activeChatId}
            classes={{ item: "chat-item", link: "chat-item-title", menu: "chat-item-menu" }}
            onOpenChat={onOpenChat}
            onDeleteChat={onDeleteChat}
            onRenameChat={onRenameChat}
            onPinChat={onPinChat}
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

export function ChatMessage({
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
  agentRun,
  agentEntries,
  agentBusy,
  agentPatch,
  onAgentDecide,
  onAgentDeliver,
  onAgentUndo,
  onCloseAgentPatch,
}) {
  const isUser = message.role === "user";
  const hasThinking = Boolean(message.thinking?.trim());
  const isEditing = isUser && editingMessageId === message.id;
  const previousUser = isUser ? null : previousUserMessage(messages, message);
  const metadataItems = isUser
    ? []
    : [formatResponseKind(message), formatTokens(message), formatDuration(message.duration_ms)]
      .filter(Boolean);

  const sentAt = formatMessageTime(message.created_at);
  // An agent turn is an assistant turn that did some work first. The work is
  // drawn above the bubble; the bubble itself holds the answer, exactly as it
  // does for a reply, which is what makes the two read as one conversation.
  const isAgentTurn = message.response_kind === "agent_run";
  const run = isAgentTurn ? agentRun : null;
  const delivery = run?.delivery;
  const hasChanges = Boolean(delivery?.deliverable?.length || delivery?.blocked?.length);
  const canUndo = hasChanges && delivery?.mode === "live" && Boolean(delivery?.undoable);
  // While the run works the answer has not been written yet, so there is no
  // bubble to show -- the trace above is the whole turn.
  const hideEmptyBubble = isAgentTurn && !message.content?.trim();

  return (
    <article className={`neo-chat-message ${isUser ? "user" : "assistant"}${isAgentTurn ? " agent-turn-message" : ""}`}>
      <div className="message-stack">
        <span className="message-sender">{isUser ? "You" : "Neo"}</span>
        {isAgentTurn ? (
          <AgentTurn
            run={run}
            entries={agentEntries}
            traceOpen={thinkingOpen}
            busy={agentBusy}
            onDecide={onAgentDecide}
            patch={agentPatch}
            onClosePatch={onCloseAgentPatch}
          />
        ) : null}
        {hideEmptyBubble ? null : (
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
            {/* The turn's own record -- when it was sent, what answered, and what
                you can do with it -- rides inside the bubble it belongs to. */}
            <div className="message-footer">
              {sentAt ? <time className="message-time">{sentAt}</time> : null}
              {metadataItems.length > 0 ? (
                <span className="message-meta">
                  {metadataItems.map((item) => <span key={item}>{item}</span>)}
                </span>
              ) : null}
              <MessageActionsMenu label={isUser ? "Message actions" : "Response actions"}>
                <button type="button" onClick={() => onCopy(message.content)}>
                  Copy
                </button>
                {isUser ? (
                  <button type="button" onClick={() => onEdit(message)}>
                    Edit
                  </button>
                ) : isAgentTurn ? (
                  <>
                    <button type="button" onClick={() => onToggleThinking(message.id)}>
                      {thinkingOpen ? "Hide thinking" : "View thinking"}
                    </button>
                    {/* Only offered when this run actually produced changes, so
                        the menu never shows an action that would do nothing. */}
                    {hasChanges ? (
                      <button type="button" onClick={() => onAgentDeliver?.(run, "patch")}>
                        View diff
                      </button>
                    ) : null}
                    {hasChanges && delivery?.mode !== "live" && delivery?.deliverable?.length ? (
                      <button type="button" onClick={() => onAgentDeliver?.(run, "working_tree")}>
                        Apply changes
                      </button>
                    ) : null}
                    {canUndo ? (
                      <button
                        type="button"
                        className="row-actions-danger"
                        onClick={() => onAgentUndo?.(run)}
                      >
                        Undo this run
                      </button>
                    ) : null}
                  </>
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
              </MessageActionsMenu>
            </div>
            {!isUser && !isAgentTurn && thinkingOpen && (
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
        )}
      </div>
    </article>
  );
}

export function PendingAssistantMessage({ generation, elapsedMs }) {
  const hasThinking = Boolean(generation?.thinking);
  const hasContent = Boolean(generation?.content);

  return (
    <article className="neo-chat-message assistant thinking">
      <div className="message-stack">
        <span className="message-sender">Neo</span>
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

/** A folder is not a file, so the action that opens one gets its own glyph. */
function FolderPlusIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 20a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1h5l2 2h8a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1z" />
      <path d="M12 11v6" />
      <path d="M9 14h6" />
    </svg>
  );
}

function WrenchIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14.7 6.3a4 4 0 0 0-5.6 4.9L3 17.3l1.4 1.4 6-6.1a4 4 0 0 0 4.9-5.6z" />
    </svg>
  );
}

/** Attaching files reads the same in both modes, so it is written once. */
function AttachFilesAction({ attaching, disabled, onPick }) {
  return (
    <button
      type="button"
      className="composer-menu-action chat-attach-button"
      onClick={onPick}
      disabled={disabled || attaching}
      title="Attach files"
      aria-label="Attach files"
    >
      <PaperclipIcon />
      <span>{attaching ? "Attaching…" : "Attach files"}</span>
    </button>
  );
}

function SubmitArrowIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M12 19V5" />
      <path d="m5 12 7-7 7 7" />
    </svg>
  );
}

/**
 * The composer is one rounded terminal-green card: the objective/message on the
 * first line with the model picker opposite it, and a control row underneath --
 * tools on the left, mode switch and send on the right.
 */
export function ChatComposer({
  disabled,
  value,
  onChange,
  onSubmit,
  llms,
  llmId,
  onLlmChange,
  mode,
  onModeChange,
  agentDefinitions,
  selectedAgentDefinitionId,
  onAgentDefinitionChange,
  repos = [],
  selectedRepoId,
  onRepoChange,
  agentMode = "normal",
  onAgentModeChange,
  onOpenFolder,
  folderAttaching = false,
  onOpenToolsPanel,
  agentMessage,
  attachments = [],
  onAttachFiles,
  onRemoveAttachment,
  attaching = false,
  attachError = "",
  generating = false,
  onStop,
  stopping = false,
  steering = false,
}) {
  const textareaRef = useRef(null);
  const attachInputRef = useRef(null);
  // Everything the run needs but the objective itself lives behind the "+":
  // repo, permission mode, agent, and whatever the clip attaches.
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);
  const menuButtonRef = useRef(null);

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

  // The menu is a popover, so it closes the way one is expected to: escape, or a
  // click anywhere that is not the menu or the button that opened it.
  useEffect(() => {
    if (!menuOpen) {
      return undefined;
    }

    function onPointerDown(event) {
      if (menuRef.current?.contains(event.target) || menuButtonRef.current?.contains(event.target)) {
        return;
      }
      setMenuOpen(false);
    }

    function onKeyDown(event) {
      if (event.key === "Escape") {
        setMenuOpen(false);
        menuButtonRef.current?.focus();
      }
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  // Mode switches carry no context with them, so a menu left open would be
  // showing another mode's controls.
  useEffect(() => {
    setMenuOpen(false);
  }, [mode]);

  return (
    <div className={`chat-input-wrap ${mode === "agent" ? "agent-mode" : "chatbot-mode"}`}>
      <div className="chat-input-shell">
        {attachments.length > 0 ? (
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
        {attachError ? (
          <div className="chat-attach-error">{attachError}</div>
        ) : null}
        <form className="chat-input-form" onSubmit={onSubmit}>
          <div className="composer-head">
            <div className="composer-tools">
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
                ref={menuButtonRef}
                type="button"
                className={`composer-tool composer-menu-button${menuOpen ? " is-open" : ""}`}
                onClick={() => setMenuOpen((open) => !open)}
                aria-expanded={menuOpen}
                aria-haspopup="true"
                aria-controls="composer-menu"
                aria-label={menuOpen ? "Close the composer menu" : "Open the composer menu"}
                title="More"
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                  strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                  <path d="M12 5v14" />
                  <path d="M5 12h14" />
                </svg>
              </button>
              <div
                ref={menuRef}
                id="composer-menu"
                className="composer-menu"
                aria-label="Composer options"
                hidden={!menuOpen}
              >
                {mode === "agent" ? (
                  <>
                    <label className="agent-chip">
                      <span className="agent-chip-label">Repo</span>
                      <select value={selectedRepoId} onChange={(event) => onRepoChange(event.target.value)}
                        disabled={disabled} aria-label="Select repository for agent"
                        title="The folder agent turns in this chat work on. Without one they can still search, fetch and remember.">
                        <option value="">No repository</option>
                        {repos.map((repo) => <option key={repo.id} value={repo.id}>{repo.name}</option>)}
                      </select>
                    </label>
                    <label className="agent-chip">
                      <span className="agent-chip-label">Mode</span>
                      <select value={agentMode} onChange={(event) => onAgentModeChange(event.target.value)}
                        disabled={disabled} aria-label="Select permission mode">
                        <option value="plan">Plan · propose only</option>
                        <option value="normal">Normal · ask first</option>
                        <option value="auto">Auto · no prompts</option>
                      </select>
                    </label>
                    <label className="agent-chip">
                      <span className="agent-chip-label">Agent</span>
                      <select value={selectedAgentDefinitionId} onChange={(event) => onAgentDefinitionChange(event.target.value)}
                        disabled={disabled} aria-label="Select agent definition">
                        <option value="general">General</option>
                        {agentDefinitions.filter((agent) => agent.name !== "general").map((agent) => <option key={agent.id} value={agent.id}>{agent.display_name || agent.name}</option>)}
                      </select>
                    </label>
                    <button
                      type="button"
                      className="composer-menu-action agent-folder-button"
                      onClick={() => {
                        setMenuOpen(false);
                        onOpenFolder?.();
                      }}
                      disabled={disabled || folderAttaching}
                      title="Open a folder on this computer. The agent edits it directly."
                      aria-label={folderAttaching ? "Opening a folder" : "Open a folder"}
                    >
                      <FolderPlusIcon />
                      <span>{folderAttaching ? "Opening a folder…" : "Open a folder"}</span>
                    </button>
                    <AttachFilesAction
                      attaching={attaching}
                      disabled={disabled}
                      onPick={() => {
                        setMenuOpen(false);
                        attachInputRef.current?.click();
                      }}
                    />
                    <button
                      type="button"
                      className="composer-menu-action agent-tools-button"
                      onClick={() => {
                        setMenuOpen(false);
                        onOpenToolsPanel?.();
                      }}
                      disabled={disabled}
                      title="Choose which tools the agent can use in this chat, or add a new one."
                      aria-label="Tools"
                    >
                      <WrenchIcon />
                      <span>Tools</span>
                    </button>
                  </>
                ) : (
                  <AttachFilesAction
                    attaching={attaching}
                    disabled={disabled}
                    onPick={() => {
                      setMenuOpen(false);
                      attachInputRef.current?.click();
                    }}
                  />
                )}
              </div>
            </div>
            <textarea
              ref={textareaRef}
              value={value}
              onChange={(event) => {
                onChange(event.target.value);
                requestAnimationFrame(resizeComposer);
              }}
              onInput={resizeComposer}
              placeholder={
                steering
                  ? "Steer the agent …"
                  : mode === "agent"
                    ? "What should the agent work on?"
                    : "Message Neo …"
              }
              rows={1}
              disabled={disabled}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  event.currentTarget.form?.requestSubmit();
                }
              }}
            />
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
              <svg className="chat-llm-caret" viewBox="0 0 24 24" fill="none" stroke="currentColor"
                strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="m6 15 6-6 6 6" />
              </svg>
            </div>
          </div>
          <div className="composer-foot">
            <div className="composer-actions">
              <div className="chat-mode-switch" role="tablist" aria-label="Interaction mode">
                <button type="button" role="tab" aria-selected={mode === "chatbot"}
                  className={mode === "chatbot" ? "active" : ""} onClick={() => onModeChange("chatbot")}>Chat</button>
                <button type="button" role="tab" aria-selected={mode === "agent"}
                  className={mode === "agent" ? "active" : ""} onClick={() => onModeChange("agent")}>Agent</button>
              </div>
              {/* One control for both kinds of turn. The toggle above decides
                  what the next one does, not what the button is called -- and a
                  turn already running is stopped the same way whichever it is. */}
              {generating ? (
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
                  aria-label="Send message" title="Send message">
                  <SubmitArrowIcon />
                </NeoButton>
              )}
            </div>
          </div>
        </form>
        {mode === "agent" && agentMessage ? <div className="agent-mode-message">{agentMessage}</div> : null}
      </div>
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

function SettingsDialog({ onOpenAccount, onOpenLLMs, onOpenProviderRuntime, onOpenEvaluationHarness, onOpenWorkspaceOrchestration, onOpenContinuity, onOpenRules, onOpenAgents, onOpenBundles, onOpenFiles, onOpenGitHub, onOpenRepos, onOpenContextMemory, onOpenMemoryRetrieval, onOpenReliableWebSearch, onOpenCommandSandbox, onOpenLsp, onOpenMemory, onOpenNotes, onOpenProjects, onOpenResearch, onOpenTasks, onOpenWebSearch, onClose }) {
  const groups = [
    {
      title: "Intelligence",
      icon: "memory",
      description: "Models, behavior, and agent configuration.",
      items: [
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
        ["Files", "Uploaded and generated workspace files", onOpenFiles],
        ["Repositories", "Registered code repositories", onOpenRepos],
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

//: Statuses in which a run is still going, so a reload has to rejoin it.
const ACTIVE_RUN_STATUSES = new Set(["queued", "running", "waiting_approval"]);

/**
 * The stored run, brought up to date by whatever is streaming.
 *
 * The thread payload describes a run as it stood when the chat was loaded, which
 * for a run still working is immediately out of date. The live state is layered
 * over it rather than replacing it, because only some of a run arrives as events
 * -- delivery and grants are read once, when it finishes.
 */
function mergeLiveRun(run, live, messageId) {
  if (!live.sessionId || live.sessionId !== run.session?.id) return run;
  return {
    ...run,
    session: {
      ...run.session,
      status: live.sessionStatus || run.session.status,
      todo: live.todo ?? run.session.todo,
    },
    pending_approval: live.approval ?? null,
    liveEntries: live.messageId === messageId || !live.messageId ? live.entries : null,
  };
}

function NeoApp({ profile, onProfileUpdated, onSwitchProfile }) {
  const [sidebar, setSidebar] = useState(EMPTY_SIDEBAR);
  const [activeChat, setActiveChat] = useState(null);
  // Held in a ref, not state, so a second click in the same tick is refused before
  // React has had a chance to re-render the disabled button.
  const sendGuardRef = useRef(createSendGuard());

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
  const [showBundles, setShowBundles] = useState(false);
  const [showGitHub, setShowGitHub] = useState(false);
  const [showContextMemory, setShowContextMemory] = useState(false);
  const [showMemoryRetrieval, setShowMemoryRetrieval] = useState(false);
  const [showCommandSandbox, setShowCommandSandbox] = useState(false);
  const [showLsp, setShowLsp] = useState(false);
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

  // Releasing here ties the lock to the rendered state, so it is exactly a synchronous
  // prefix of `sending`: every path that ends a send frees it, and none can leave it
  // stuck.
  useEffect(() => {
    if (!sending) {
      sendGuardRef.current.release();
    }
  }, [sending]);
  const [stopping, setStopping] = useState(false);
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
  // What the next turn will be. A per-message choice, not a view: the thread
  // stays where it is either way.
  const [chatMode, setChatMode] = useState("chatbot");
  const [agentDefinitions, setAgentDefinitions] = useState([]);
  const [agentRepos, setAgentRepos] = useState([]);
  // Which turn is in flight, so Stop knows what to stop. Set at send rather than
  // read off the stream, because the gap between the POST and the first event is
  // exactly when a user reaches for Stop.
  const [activeTurn, setActiveTurn] = useState(null);
  const [agentBusy, setAgentBusy] = useState(false);
  const [agentPatch, setAgentPatch] = useState("");
  // Where to tail this chat's log from. The server decides it -- only it knows
  // whether a turn is still running -- so it arrives with the thread.
  const [streamAfter, setStreamAfter] = useState(0);
  // A device preference rather than profile state, so it is deliberately not in
  // PROFILE_SCOPED_STORAGE_KEYS and survives a profile switch.
  const [sidebarCollapsed, setSidebarCollapsed] = useState(() => {
    try {
      return window.localStorage.getItem("neo-sidebar-collapsed") === "1";
    } catch {
      return false;
    }
  });
  const [chatAgentMessage, setChatAgentMessage] = useState("");
  const [showOpenFolder, setShowOpenFolder] = useState(false);
  const [showChatTools, setShowChatTools] = useState(false);
  const bootstrapped = useRef(false);
  const createChatPromiseRef = useRef(null);
  const visibleChatIdRef = useRef(null);
  // The chat the stream is attached to. Kept apart from `visibleChatIdRef`,
  // which goes null whenever another view is on screen: a turn that finishes
  // while the user is looking at Tasks still has to be written into the thread.
  const streamChatIdRef = useRef(null);
  streamChatIdRef.current = activeChat?.id ?? null;

  const refreshSidebar = useCallback(async () => {
    const nextSidebar = await api.sidebar();
    setSidebar(nextSidebar);
    return nextSidebar;
  }, []);

  const loadAgentContext = useCallback(async () => {
    try {
      const [agentData, repoData] = await Promise.all([
        api.agentDefinitions(false),
        api.reposList({ limit: 100 }).catch(() => ({ repos: [] })),
      ]);
      setAgentDefinitions(agentData.definitions || []);
      setAgentRepos(repoData.repos || []);
    } catch (error) {
      setStatusError(`Could not load Agent mode context: ${errorMessage(error)}`);
    }
  }, []);

  useEffect(() => {
    if (chatMode === "agent") loadAgentContext();
  }, [chatMode, loadAgentContext]);

  // A chat's badge would otherwise read RUNNING until something else happened to
  // refresh the sidebar. Poll only while a run is actually unfinished.
  const hasUnfinishedRun = (sidebar.chats || []).some((chat) =>
    ["queued", "running", "waiting_approval"].includes(chat.agent_status),
  );
  useEffect(() => {
    if (!hasUnfinishedRun) return undefined;
    const timer = window.setInterval(() => refreshSidebar().catch(() => {}), 8000);
    return () => window.clearInterval(timer);
  }, [hasUnfinishedRun, refreshSidebar]);

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
    // The transcript already carries any finished agent turn, so the only thing
    // left to establish is where to start watching -- and whether something is
    // still going, which is what a reload has to rejoin rather than orphan.
    setStreamAfter(thread.stream_after || 0);
    const runningTurn = thread.messages.find(
      (message) => message.agent && ACTIVE_RUN_STATUSES.has(message.agent.session?.status),
    );
    if (runningTurn) {
      setActiveTurn({ kind: "agent", sessionId: runningTurn.agent.session.id });
      setSending(true);
      setGenerationStartedAt(parseNeoTimestamp(runningTurn.agent.session.started_at) || Date.now());
      return thread;
    }
    const generation = await api.activeChatGeneration(thread.chat.id).catch(() => null);
    if (generation) {
      setActiveTurn({ kind: "chat", generationId: generation.id });
      setSending(true);
      setGenerationStartedAt(parseNeoTimestamp(generation.started_at || generation.created_at) || Date.now());
    } else {
      setActiveTurn(null);
      setSending(false);
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

  /**
   * One live connection for the whole thread.
   *
   * A turn ends by writing a message row, so the transcript is reloaded and the
   * live state stands down rather than lingering as a second copy of what is now
   * on the page. This is the same handling for both kinds, because by the time a
   * turn is over the difference between them is only what the row says.
   */
  const handleTurnEnd = useCallback(
    async (event) => {
      const chatId = streamChatIdRef.current;
      if (!chatId) return;
      if (event?.type === "run.failed" && event.error) setStatusError(event.error);
      try {
        await loadChat(chatId, { history: "none" });
      } catch (error) {
        setStatusError(`Could not reload the chat: ${errorMessage(error)}`);
      }
      setActiveTurn(null);
      setSending(false);
      setStopping(false);
      setGenerationStartedAt(null);
      refreshSidebar().catch(() => {});
    },
    [loadChat, refreshSidebar],
  );

  const { live } = useChatStream(activeChat?.id ?? null, streamAfter, { onTurnEnd: handleTurnEnd });

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

  async function handlePinChat(chat, pinned) {
    setStatusError("");
    try {
      const updated = await api.pinChat(chat.id, pinned);
      setActiveChat((current) => (current?.id === chat.id ? { ...current, ...updated } : current));
      await refreshSidebar();
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
    const target = pendingDelete;
    setStatusError("");

    try {
      if (target.type === "chat") {
        await api.deleteChat(target.id, { memoryEnabled, memoryIncognito });
      } else {
        await api.deleteProject(target.id);
      }
    } catch (error) {
      setStatusError(errorMessage(error));
      return;
    }

    // The row is gone, so the dialog has done its job. Closing it here rather than
    // after the follow-up below is what stops a failure in that follow-up from leaving
    // a confirmed deletion on screen, which read as "delete did nothing" and invited
    // repeated DELETEs for a row that no longer existed.
    setPendingDelete(null);

    try {
      if (target.type === "chat") {
        if (activeChat?.id === target.id) {
          await createActiveChat(selectedProjectId);
        }
      } else {
        if (selectedProjectId === target.id || activeChat?.project_id === target.id) {
          await createActiveChat(null);
        }
        setSelectedProjectId(null);
      }
    } catch (error) {
      setStatusError(errorMessage(error));
    } finally {
      // Opening a replacement chat is a convenience; the sidebar still has to lose the
      // deleted row even when that convenience fails.
      try {
        await refreshSidebar();
      } catch (error) {
        setStatusError(errorMessage(error));
      }
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
        createRequestId(),
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
      setActiveTurn({ kind: "chat", generationId: result.generation.id });
      setGenerationStartedAt(parseNeoTimestamp(result.generation.created_at) || Date.now());
      setElapsedMs(0);
    } catch (error) {
      setStatusError(errorMessage(error));
    }
  }

  /**
   * Start the next turn, of whichever kind the composer asked for.
   *
   * One path for both: the send, the guard against a double click, the optimistic
   * user bubble and the failure handling are the same work either way. Only the
   * `mode` differs, and what the server does with it.
   */
  async function sendPrompt(prompt, turnMode = "chat") {
    // `sending` catches a send started by another flow (a rerun, or a generation resumed
    // after reload). It cannot catch a second click in the same tick, because it only
    // becomes true on the next render -- that window is what the guard closes.
    if (!prompt || sending) {
      return;
    }
    const requestId = sendGuardRef.current.begin();
    if (requestId === null) {
      return;
    }

    setSending(true);
    setStatusError("");
    setChatAgentMessage("");
    setGenerationStartedAt(Date.now());
    setElapsedMs(0);
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
      const result = await api.startChatGeneration(
        chat.id,
        prompt,
        selectedLlmId || null,
        requestId,
        {
          ...browserChatContext(),
          memoryEnabled,
          memoryIncognito,
          mode: turnMode,
          repoId: chat.repo_id ?? null,
          agentMode: chat.agent_mode ?? null,
          agentDefinitionId: chat.agent_definition_id ?? null,
        },
      );
      if (result.agent_session_id) {
        setActiveTurn({ kind: "agent", sessionId: result.agent_session_id });
        // The run wrote a row to hold its place in the transcript, and the trace
        // draws into that row -- so the thread is reloaded rather than waiting
        // for the first event to imply a turn that is already there.
        await loadChat(chat.id, { history: "none" });
        setSending(true);
        refreshSidebar().catch(() => {});
      } else {
        setActiveTurn({ kind: "chat", generationId: result.generation.id });
      }
    } catch (error) {
      setMessages((current) =>
        current.map((message) =>
          message.id === pendingId ? { ...message, failed: true } : message,
        ),
      );
      setComposerValue(prompt);
      setStatusError(`${errorMessage(error)}. Your message was not sent, but it was kept.`);
      setSending(false);
      setActiveTurn(null);
      setGenerationStartedAt(null);
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

  /** Stop whatever is running, whichever kind it is. */
  async function handleStopGeneration() {
    if (!activeTurn || !activeChat?.id || stopping) return;
    setStopping(true);
    try {
      if (activeTurn.kind === "agent") await api.cancelAgentSession(activeTurn.sessionId);
      else await api.cancelChatGeneration(activeChat.id, activeTurn.generationId);
      // The tail sees the cancellation and tears the live state down.
    } catch (error) {
      setStopping(false);
      setStatusError(`Could not stop the response: ${errorMessage(error)}`);
    }
  }

  async function handleSendMessage(event) {
    event.preventDefault();
    const prompt = composerValue.trim();
    if (!prompt) {
      return;
    }
    const outgoing = promptWithAttachments(prompt);
    // Typing while a run is working steers it rather than queueing a second
    // turn: the correction lands before the agent's next decision, which is the
    // whole point of being able to say something mid-run.
    if (steeringSessionId) {
      setComposerValue("");
      setChatAttachments([]);
      setAttachError("");
      try {
        await api.sendAgentMessage(steeringSessionId, outgoing);
      } catch (error) {
        setComposerValue(prompt);
        setStatusError(`Could not send that to the agent: ${errorMessage(error)}`);
      }
      return;
    }
    if (sending) {
      return;
    }
    setComposerValue("");
    setChatAttachments([]);
    setAttachError("");
    await sendPrompt(outgoing, chatMode === "agent" ? "agent" : "chat");
  }

  /** Persist a composer chip onto the chat it belongs to. */
  async function updateChatAgentSettings(changes) {
    setChatAgentMessage("");
    try {
      const chat = activeChat ?? (await createActiveChat(selectedProjectId, { resetMessages: false }));
      const updated = await api.updateChat(chat.id, changes);
      setActiveChat((current) => (current?.id === updated.id ? { ...current, ...updated } : current));
    } catch (error) {
      setChatAgentMessage(`Could not save that: ${errorMessage(error)}`);
    }
  }

  async function handleAgentDecide(decision, predicate) {
    const run = pendingApprovalRun;
    if (!run) return;
    setAgentBusy(true);
    setStatusError("");
    try {
      await api.decideAgentApproval(run.session.id, run.pending_approval.id, decision, predicate);
      await loadChat(activeChat.id, { history: "none" });
    } catch (error) {
      setStatusError(errorMessage(error));
    } finally {
      setAgentBusy(false);
    }
  }

  async function handleAgentDeliver(run, deliverMode) {
    if (!run?.session?.id) return;
    setAgentBusy(true);
    try {
      const result = await api.deliverAgentChanges(run.session.id, { mode: deliverMode });
      if (deliverMode === "patch") setAgentPatch(result.patch || "(no changes)");
      else setChatAgentMessage(`Wrote ${result.written?.length ?? 0} file(s) into your repository.`);
    } catch (error) {
      setChatAgentMessage(errorMessage(error));
    } finally {
      setAgentBusy(false);
    }
  }

  async function handleAgentUndo(run) {
    if (!run?.session?.id) return;
    if (!window.confirm("Undo every change this run made to your folder?")) return;
    setAgentBusy(true);
    try {
      const result = await api.undoAgentRun(run.session.id);
      const reversed = (result.restored?.length ?? 0) + (result.removed?.length ?? 0);
      const skipped = result.skipped || [];
      setAgentPatch("");
      setChatAgentMessage(
        skipped.length
          ? `Undid ${reversed} file(s). Left alone: ${skipped
            .map((item) => item.path)
            .join(", ")} — changed after the run finished.`
          : `Undid ${reversed} file(s).`,
      );
      await loadChat(activeChat.id, { history: "none" });
    } catch (error) {
      setChatAgentMessage(errorMessage(error));
    } finally {
      setAgentBusy(false);
    }
  }

  /**
   * Open the run a task links to, by opening the conversation it happened in.
   *
   * A run has no view of its own any more, so "open this run" means "go to that
   * turn of that chat" -- which is also why it is now a normal permalink.
   */
  async function handleOpenAgentSession(sessionId) {
    setStatusError("");
    try {
      const detail = await api.agentSession(sessionId);
      const chatId = detail.session?.chat_id;
      if (!chatId) {
        setStatusError("That run is not part of a conversation, so there is nothing to open.");
        return;
      }
      setShowTasks(false);
      await handleOpenChat(chatId);
    } catch (error) {
      setStatusError(`Could not open that run: ${errorMessage(error)}`);
    }
  }

  function handleOpenFolder() {
    setChatAgentMessage("");
    setShowOpenFolder(true);
  }

  // The repo chip shows what was opened, so the attach says nothing further;
  // the composer's message line is left for failures. The folder is saved onto
  // the chat, so every agent turn in this conversation works on it.
  async function handleFolderAttached(repo) {
    await loadAgentContext();
    if (repo?.id) await updateChatAgentSettings({ repo_id: repo.id });
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

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((current) => {
      const next = !current;
      try {
        window.localStorage.setItem("neo-sidebar-collapsed", next ? "1" : "0");
      } catch {
        /* storage is unavailable in private mode; the choice just does not persist */
      }
      return next;
    });
  }, []);

  const showEmptyState = messages.length === 0 && !sending;
  // The run each agent turn is, keyed by the row that holds its place, with
  // whatever is currently streaming laid over the top: the thread payload is a
  // snapshot from when the chat was loaded, and a run moves on from there.
  const agentRuns = useMemo(() => {
    const byMessage = {};
    for (const message of messages) {
      if (!message.agent) continue;
      byMessage[message.id] = mergeLiveRun(message.agent, live, message.id);
    }
    return byMessage;
  }, [messages, live]);
  const pendingApprovalRun = useMemo(
    () => Object.values(agentRuns).find((run) => run?.pending_approval) || null,
    [agentRuns],
  );
  // A run that is working takes what you type as steering, not as a new turn.
  // One waiting on approval does not: it is waiting on a decision, and the
  // buttons are the answer.
  const steeringSessionId =
    activeTurn?.kind === "agent" && live.sessionStatus !== "waiting_approval"
      ? activeTurn.sessionId
      : null;
  // Only a plain reply gets the pending bubble; an agent turn has a row of its
  // own to draw into, so a second placeholder would be the same turn twice.
  const streamingAssistant = live.kind === "chat"
    ? {
      rawContent: live.text,
      ...splitGeneratedText(live.text),
      thinking: live.thinking || splitGeneratedText(live.text).thinking,
      statusDetail: live.statusText,
    }
    : null;
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
    <div className={`neo-app${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
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
        onPinChat={handlePinChat}
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
        onOpenTasks={() => {
          setInitialTaskId(null); setInitialTaskProjectId(null); setShowResearch(false); setShowNotes(false); setShowProjects(false); setShowFiles(false); setShowRepos(false); setShowTasks(true);
        }}
        activeView={activeView}
        profile={profile}
        onSwitchProfile={onSwitchProfile}
        collapsed={sidebarCollapsed}
        onToggleCollapsed={toggleSidebar}
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
          onOpenAgentSession={handleOpenAgentSession}
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
          <span className="neo-view-context">{chatMode === "agent" ? "Agent Mode" : "Chat Mode"}</span>
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
              agentRun={agentRuns[message.id]}
              agentEntries={agentRuns[message.id]?.liveEntries}
              agentBusy={agentBusy}
              agentPatch={agentPatch}
              onAgentDecide={handleAgentDecide}
              onAgentDeliver={handleAgentDeliver}
              onAgentUndo={handleAgentUndo}
              onCloseAgentPatch={() => setAgentPatch("")}
            />
          ))}

          {streamingAssistant && (
            <PendingAssistantMessage generation={streamingAssistant} elapsedMs={elapsedMs} />
          )}

          {statusError && <div className="neo-error">{statusError}</div>}
        </section>

        <ChatComposer
          /* Stop is offered whenever something is running, and steering is not
             something to stop -- a run waiting to be steered is waiting on you. */
          generating={sending && Boolean(activeTurn) && !steeringSessionId}
          onStop={handleStopGeneration}
          stopping={stopping}
          steering={Boolean(steeringSessionId)}
          attachments={chatAttachments}
          onAttachFiles={handleAttachFiles}
          onRemoveAttachment={handleRemoveAttachment}
          attaching={attachingFiles}
          attachError={attachError}
          value={composerValue}
          onChange={setComposerValue}
          onSubmit={handleSendMessage}
          /* Never disabled by a missing workspace: an agent turn without one
             runs with the folder tools withheld rather than being refused. */
          disabled={sending && !steeringSessionId}
          llms={llms}
          llmId={selectedLlmId}
          onLlmChange={handleLlmChange}
          mode={chatMode}
          onModeChange={setChatMode}
          repos={agentRepos}
          selectedRepoId={activeChat?.repo_id || ""}
          onRepoChange={(repoId) => updateChatAgentSettings({ repo_id: repoId })}
          agentDefinitions={agentDefinitions}
          selectedAgentDefinitionId={activeChat?.agent_definition_id || "general"}
          onAgentDefinitionChange={(id) => updateChatAgentSettings({ agent_definition_id: id })}
          agentMode={activeChat?.agent_mode || "normal"}
          onAgentModeChange={(nextMode) => updateChatAgentSettings({ agent_mode: nextMode })}
          onOpenFolder={handleOpenFolder}
          folderAttaching={false}
          onOpenToolsPanel={() => setShowChatTools(true)}
          agentMessage={chatAgentMessage}
        />
        {showOpenFolder && (
          <OpenFolderDialog
            projectId={null}
            onClose={() => setShowOpenFolder(false)}
            onAttached={handleFolderAttached}
          />
        )}
        {showChatTools && activeChat?.id && (
          <ChatToolsPanel chatId={activeChat.id} onClose={() => setShowChatTools(false)} />
        )}
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
          onOpenRules={() => { setShowSettings(false); setShowRulesSettings(true); }}
          onOpenAgents={() => { setShowSettings(false); setShowAgentSettings(true); }}
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
          onOpenFiles={() => {
            setShowSettings(false);
            setInitialFileId(null);
            setShowResearch(false);
            setShowNotes(false);
            setShowProjects(false);
            setShowTasks(false);
            setShowRepos(false);
            setShowFiles(true);
          }}
          onOpenRepos={() => {
            setShowSettings(false);
            setShowResearch(false);
            setShowNotes(false);
            setShowProjects(false);
            setShowTasks(false);
            setShowFiles(false);
            setShowRepos(true);
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
      {showBundles && <Modal title="Bundles" onClose={() => setShowBundles(false)} wide><Bundles /></Modal>}
      {showGitHub && <Modal title="GitHub" onClose={() => setShowGitHub(false)} wide><GitHub onClose={() => setShowGitHub(false)} /></Modal>}
      {showContextMemory && <Modal title="Context Memory" onClose={() => setShowContextMemory(false)} wide><ContextMemory /></Modal>}
      {showMemoryRetrieval && <Modal title="Workspace Retrieval" onClose={() => setShowMemoryRetrieval(false)} wide><MemoryRetrieval /></Modal>}
      {showCommandSandbox && <Modal title="Command Sandbox" onClose={() => setShowCommandSandbox(false)} wide><CommandSandbox /></Modal>}
      {showLsp && <Modal title="Language Server Protocol" onClose={() => setShowLsp(false)} wide><LspPanel /></Modal>}
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

// Every key here holds state that belongs to one profile. They live in
// localStorage, which is scoped to the origin rather than to the profile, so a
// profile transition has to drop them explicitly — otherwise the next profile
// boots pointing at rows that only exist in the previous profile's store.
// An agent run is a turn of a chat, so the chat id is the only thing worth
// remembering: reopening it reattaches to a run still executing on the server.
export const PROFILE_SCOPED_STORAGE_KEYS = ["neo-active-chat-id"];

export function clearProfileScopedState() {
  try {
    for (const key of PROFILE_SCOPED_STORAGE_KEYS) localStorage.removeItem(key);
  } catch {
    /* storage is unavailable in private mode; nothing was persisted to clear */
  }
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
      clearProfileScopedState();
      window.location.assign("/");
    }
  }

  if (checkingSession) {
    return <main className="profile-picker"><p className="profile-loading">Loading profiles…</p></main>;
  }
  if (!profile) {
    return (
      <ProfilePicker
        onSignedIn={(next) => {
          clearProfileScopedState();
          setProfile(next);
        }}
      />
    );
  }
  return <NeoApp profile={profile} onProfileUpdated={setProfile} onSwitchProfile={switchProfile} />;
}
