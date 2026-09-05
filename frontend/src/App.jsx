import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { api } from "./api.js";
import { createRequestId, createSendGuard } from "./sendGuard.js";
import { MessageActionsMenu } from "./MessageActionsMenu.jsx";
import { ContextWindowIndicator } from "./ContextWindowIndicator.jsx";
import AgentTurn, { TERMINAL } from "./AgentTurn.jsx";
import { IDLE, useChatStreams } from "./chatStream.js";
import BackgroundTurnToast, {
  NOTIFY_STORAGE_KEY,
  notificationsEnabled,
  shouldNotify,
} from "./BackgroundTurnToast.jsx";
import { PaperclipIcon } from "./icons.jsx";
import { registerModal } from "./modalStack.js";
import OpenFolderDialog from "./OpenFolderDialog.jsx";
import ChatToolsPanel from "./ChatToolsPanel.jsx";
import ExternalAgents from "./ExternalAgents.jsx";
import Notes from "./Notes.jsx";
import WorkspaceIcon from "./WorkspaceIcon.jsx";
import Projects from "./Projects.jsx";
import Research from "./Research.jsx";
import Tasks from "./Tasks.jsx";
import Calendar from "./Calendar.jsx";
import CalendarProposalCard from "./CalendarProposalCard.jsx";
import ReminderToast from "./ReminderToast.jsx";
import Files from "./Files.jsx";
import Gallery from "./Gallery.jsx";
import GalleryImages from "./GalleryImages.jsx";
import ImageLightbox from "./ImageLightbox.jsx";
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
  formatOutputTokens,
  formatResponseKind,
  parseNeoTimestamp,
  renderMessageHtml,
  resolveContextWindow,
  splitGeneratedText,
  sumTotalTokens,
} from "./chatPresentation.js";

// The guard key for a send that is creating its chat as it goes: there is no id
// to key by yet, and two such sends are the double-click the guard exists for.
const NEW_CHAT_GUARD_KEY = "new-chat";

const EMPTY_SIDEBAR = { projects: [], chats: [] };

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
    calendar: ["M5 4h14v16H5z", "M5 9h14", "M8 2v4", "M16 2v4"],
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

const TURN_STATUS_LABELS = {
  queued: "Waiting for a free slot",
  running: "Working on a reply",
  waiting_approval: "Waiting for your approval",
  done: "Finished while you were away",
};

function SidebarChatRow({
  chat,
  href,
  isActive,
  classes,
  status,
  onOpenChat,
  onDeleteChat,
  onRenameChat,
  onPinChat,
}) {
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
        {status ? (
          <span
            className={`chat-item-badge is-${status}`}
            aria-label={TURN_STATUS_LABELS[status] || status}
            title={TURN_STATUS_LABELS[status] || status}
          />
        ) : null}
      </a>
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
  statusFor,
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
  onOpenCalendar,
  onOpenGallery,
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
    ["calendar", "Calendar", onOpenCalendar],
    ["gallery", "Gallery", onOpenGallery],
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
        <form
          className="sidebar-form"
          onSubmit={submitProject}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              setProjectName("");
              onToggleProjectForm();
            }
          }}
        >
          <label>
            <span>Project name</span>
            <input
              value={projectName}
              onChange={(event) => setProjectName(event.target.value)}
              placeholder="Research, work, ideas..."
            />
          </label>
          <div className="sidebar-form-actions">
            <NeoButton type="submit" className="sidebar-form-submit">
              Create
            </NeoButton>
            <NeoButton
              type="button"
              className="secondary"
              onClick={() => {
                setProjectName("");
                onToggleProjectForm();
              }}
            >
              Cancel
            </NeoButton>
          </div>
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
                status={statusFor?.(chat)}
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
            status={statusFor?.(chat)}
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
  onOpenCalendar,
  onOpenGalleryItem,
  onProposalResolved,
  agentRun,
  agentEntries,
  agentBusy,
  agentPatch,
  agentPatchSessionId,
  onAgentDecide,
  onAgentDeliver,
  onAgentUndo,
  onAgentFork,
  onCloseAgentPatch,
  contextWindowIndex,
  sessionTokensUsed,
}) {
  const isUser = message.role === "user";
  const hasThinking = Boolean(message.thinking?.trim());
  const isEditing = isUser && editingMessageId === message.id;
  const previousUser = isUser ? null : previousUserMessage(messages, message);
  const metadataItems = isUser
    ? []
    : [formatResponseKind(message), formatDuration(message.duration_ms), formatOutputTokens(message)]
      .filter(Boolean);

  const sentAt = formatMessageTime(message.created_at);
  // An agent turn is an assistant turn that did some work first. The work is
  // drawn above the bubble; the bubble itself holds the answer, exactly as it
  // does for a reply, which is what makes the two read as one conversation.
  const isAgentTurn = message.response_kind === "agent_run";
  const isCompactionSummary = message.response_kind === "compaction_summary";
  const calendarProposal =
    message.response_kind === "calendar_proposal" ? message.metadata?.calendar_proposal : null;
  // What this turn showed Neo. Read from the stored turn rather than from
  // composer state, so a reloaded thread still shows the picture instead of a
  // message that reads as though nothing was sent with it.
  const messageImageIds = message.metadata?.image_ids ?? [];
  const run = isAgentTurn ? agentRun : null;
  const delivery = run?.delivery;
  const hasChanges = Boolean(delivery?.deliverable?.length || delivery?.blocked?.length);
  const canUndo = hasChanges && delivery?.mode === "live" && Boolean(delivery?.undoable);
  const patchOpen = isAgentTurn && Boolean(run?.session?.id) && run.session.id === agentPatchSessionId;
  // While the run works the answer has not been written yet, so there is no
  // bubble to show -- the trace above is the whole turn.
  const hideEmptyBubble = isAgentTurn && !message.content?.trim();

  return (
    <article className={`neo-chat-message ${isUser ? "user" : "assistant"}${isAgentTurn ? " agent-turn-message" : ""}${isCompactionSummary ? " compaction-summary-message" : ""}`}>
      <div className="message-stack">
        <span className="message-sender">{isUser ? "You" : "Neo"}</span>
        {isAgentTurn ? (
          <AgentTurn
            run={run}
            entries={agentEntries}
            traceOpen={thinkingOpen}
            busy={agentBusy}
            onDecide={onAgentDecide}
            patch={patchOpen ? agentPatch : ""}
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
            {isCompactionSummary && (
              <div className="compaction-summary-header">
                <CompactIcon />
                <span>Conversation compacted</span>
                {Number.isFinite(message.metadata?.compacted_message_count) ? (
                  <span className="compaction-summary-count">
                    {message.metadata.compacted_message_count} earlier message
                    {message.metadata.compacted_message_count === 1 ? "" : "s"} summarized
                  </span>
                ) : null}
              </div>
            )}
            {/* Escaped by renderMessageHtml before any tag is emitted. */}
            <div
              className="chat-content"
              dangerouslySetInnerHTML={{ __html: renderMessageHtml(message.content) }}
            />
            {messageImageIds.length ? (
              <GalleryImages items={messageImageIds} onOpenGallery={onOpenGalleryItem} />
            ) : null}
            {calendarProposal ? (
              <CalendarProposalCard
                proposal={calendarProposal}
                messageId={message.id}
                onResolved={onProposalResolved}
                onOpenCalendar={onOpenCalendar}
              />
            ) : null}
            {message.failed && (
              <div className="chat-message-status">Not sent. Edit and try again.</div>
            )}
            {/* The turn's own record -- when it was sent, what answered, and what
                you can do with it -- rides inside the bubble it belongs to. */}
            <div className="message-footer">
              {sentAt ? <time className="message-time">{sentAt}</time> : null}
              {metadataItems.length > 0 || !isUser ? (
                <span className="message-meta">
                  {metadataItems.map((item) => <span key={item}>{item}</span>)}
                  {!isUser ? (
                    <ContextWindowIndicator
                      message={message}
                      contextWindowIndex={contextWindowIndex}
                      sessionTokensUsed={sessionTokensUsed}
                    />
                  ) : null}
                </span>
              ) : null}
              <MessageActionsMenu label={isUser ? "Message actions" : "Response actions"}>
                <button type="button" onClick={() => onCopy(message.content)}>
                  Copy
                </button>
                <button type="button" onClick={() => onAgentFork?.(message)}>
                  Fork conversation
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
                        {patchOpen ? "Hide diff" : "View diff"}
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
                ) : isCompactionSummary ? null : (
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
            {!isUser && !isAgentTurn && !isCompactionSummary && thinkingOpen && (
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

/** A shrinking bracket -- the closest glyph to "make this smaller". */
function CompactIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M9 5H5v4" />
      <path d="m5 5 6 6" />
      <path d="M15 19h4v-4" />
      <path d="m19 19-6-6" />
    </svg>
  );
}

/** Compacting reads the same in both modes, so it is written once, like AttachFilesAction. */
function CompactConversationAction({ compacting, disabled, onCompact }) {
  return (
    <button
      type="button"
      className="composer-menu-action chat-compact-button"
      onClick={onCompact}
      disabled={disabled || compacting}
      title="Summarize older messages to reduce context window usage."
      aria-label={compacting ? "Compacting conversation" : "Compact conversation"}
    >
      <CompactIcon />
      <span>{compacting ? "Compacting…" : "Compact conversation"}</span>
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
 * Which engines the picker may offer, and what each is called there.
 *
 * Only the ones that are actually usable -- installed, signed in, switched on.
 * An engine you have not signed in to is not a choice you are declining to
 * make, it is a task, and this control cannot do tasks: every other row in the
 * menu takes effect the moment it is picked, and one that instead opened a
 * browser and waited on a sign-in was the odd one out. Signing in lives in
 * Settings > Engines now, and this offers what that produced.
 *
 * The one engine offered regardless is the one this chat is *already* running
 * on. A stored executor whose CLI has since signed out or been switched off
 * would otherwise leave the select with no matching option, and a select with
 * no match silently shows its first -- telling someone their chat is on Neo
 * when the next turn will be refused. So it is offered, carrying `available:
 * false`; the composer states that in a line of its own underneath rather than
 * in the label, because the chip truncates at 160px and "Claude Code - not
 * con..." is not a thing anyone should have to finish in their head.
 *
 * Exported as a plain function because the rule is the whole point of the
 * control, and the frontend suite renders to static markup with no way to fire
 * a change event at it.
 */
export function engineOptions(executor = "neo", externalAgents = []) {
  const options = [{ id: "neo", name: "Neo", available: true }];
  for (const agent of externalAgents) {
    if (agent.available || agent.id === executor) {
      options.push({ id: agent.id, name: agent.name, available: Boolean(agent.available) });
    }
  }
  // A chat can carry an executor Neo no longer detects at all -- the CLI was
  // removed, or the profile was moved to another machine. Naming it is still
  // more honest than showing "Neo".
  if (executor !== "neo" && !options.some((option) => option.id === executor)) {
    options.push({ id: executor, name: executor, available: false });
  }
  return options;
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
  executor = "neo",
  onExecutorChange,
  externalAgents = [],
  onOpenEngineSettings,
  effort = "low",
  onEffortChange = () => {},
  onOpenFolder,
  folderAttaching = false,
  onOpenToolsPanel,
  agentMessage,
  attachments = [],
  images = [],
  onAttachFiles,
  onRemoveAttachment,
  attaching = false,
  attachError = "",
  generating = false,
  onStop,
  stopping = false,
  steering = false,
  onCompactConversation,
  compacting = false,
}) {
  const textareaRef = useRef(null);
  const attachInputRef = useRef(null);
  // Everything the run needs but the objective itself lives behind the "+":
  // repo, permission mode, agent, and whatever the clip attaches.
  // Both read off the capability contract the API returns, so the interface
  // never asserts an enforcement the CLI does not provide.
  const activeExecutor = externalAgents.find((agent) => agent.id === executor) || null;
  const toolTogglesApply = !activeExecutor || activeExecutor.capabilities?.tool_denylist !== false;
  const engineChoices = engineOptions(executor, externalAgents);
  // The engine this chat would actually run on, and whether it still can. A
  // chat outlives the engine it was pointed at -- the CLI signs out, or the
  // profile switch goes off -- and that has to be said before the next turn is
  // refused, not after.
  const activeChoice = engineChoices.find((choice) => choice.id === executor);
  const engineBroken = Boolean(activeChoice) && !activeChoice.available;
  // Whether anything beyond Neo is genuinely connected, which decides whether
  // the line below the picker is an invitation or a way back to the panel.
  const hasConnectedEngine = externalAgents.some((agent) => agent.available);
  const [menuOpen, setMenuOpen] = useState(false);
  // Which attached image is open full-screen, or -1 for none.
  const [zoomedImage, setZoomedImage] = useState(-1);
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
        {images.length > 0 ? (
          <div className="chat-attachments chat-attachment-images">
            {images.map((item, position) => (
              <span className="chat-attachment-image" key={item.id}>
                <button
                  type="button"
                  className="chat-attachment-open"
                  onClick={() => setZoomedImage(position)}
                  title={`${item.title || "Pasted image"} (click to enlarge)`}
                  aria-label={`Enlarge ${item.title || "image"}`}
                >
                  <img
                    src={api.galleryThumbnailUrl(item.id)}
                    alt={item.title || "Pasted image"}
                  />
                </button>
                <button
                  type="button"
                  className="chat-attachment-remove"
                  onClick={() => onRemoveAttachment?.(item.id)}
                  aria-label={`Remove ${item.title || "image"}`}
                >
                  ×
                </button>
              </span>
            ))}
          </div>
        ) : null}
        {zoomedImage >= 0 && images[zoomedImage] ? (
          <ImageLightbox
            images={images.map((item) => ({
              id: item.id,
              title: item.title || "",
              alt: item.alt_text || item.title || "Pasted image",
            }))}
            index={zoomedImage}
            onIndexChange={setZoomedImage}
            onClose={() => setZoomedImage(-1)}
          />
        ) : null}
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
                      <span className="agent-chip-label">Engine</span>
                      <select value={executor} onChange={(event) => onExecutorChange(event.target.value)}
                        disabled={disabled} aria-label="Select agent engine"
                        title="Which engine runs agent turns in this chat. Claude Code and Codex appear here once you have signed in to them in Settings, and run on your own CLI subscription in the attached folder.">
                        {/* Only engines that would actually run the next turn.
                            Picking one here is a switch and nothing else -- no
                            sign-in, no browser, no waiting. */}
                        {engineChoices.map((choice) => (
                          <option key={choice.id} value={choice.id}>{choice.name}</option>
                        ))}
                      </select>
                    </label>
                    <button
                      type="button"
                      className={engineBroken ? "agent-engine-link is-broken" : "agent-engine-link"}
                      onClick={() => {
                        setMenuOpen(false);
                        onOpenEngineSettings?.();
                      }}
                      /* Three jobs, one line. The picker no longer advertises
                         engines you cannot use, so this says they exist; it
                         points at the one place they are set up; and when the
                         engine this chat is on has stopped working, it says so
                         instead -- which is the only warning there would be. */
                      title="Sign in to Claude Code or Codex so they can run agent turns in this chat."
                    >
                      {engineBroken
                        ? `${activeChoice.name} is not connected — set it up in Settings`
                        : hasConnectedEngine
                          ? "Manage engines in Settings"
                          : "Connect Claude Code or Codex…"}
                    </button>
                    <label className="agent-chip">
                      <span className="agent-chip-label">Agent</span>
                      <select value={selectedAgentDefinitionId} onChange={(event) => onAgentDefinitionChange(event.target.value)}
                        disabled={disabled} aria-label="Select agent definition">
                        <option value="general">General</option>
                        {agentDefinitions.filter((agent) => agent.name !== "general").map((agent) => <option key={agent.id} value={agent.id}>{agent.display_name || agent.name}</option>)}
                      </select>
                    </label>
                    {activeExecutor?.notes?.length ? (
                      /* Collapsed. The caveats are real and stay available in
                         full, but rendered open they were the largest thing in
                         the menu and the only thing that changed on success --
                         which made a working engine look like a broken one. */
                      <details className="agent-executor-note">
                        <summary>Runs under {activeExecutor.name}&apos;s own permissions</summary>
                        <span className="agent-executor-note-list">
                          {activeExecutor.notes.map((note) => (
                            <span key={note}>{note}</span>
                          ))}
                        </span>
                      </details>
                    ) : null}
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
                      /* Not disabled when it does not apply: the toggles still
                         govern Neo's own turns in this chat, and a dead control
                         would misrepresent that. What changes is the label, so
                         the limit is stated rather than discovered. */
                      disabled={disabled}
                      title={
                        toolTogglesApply
                          ? "Choose which tools the agent can use in this chat, or add a new one."
                          : `${activeExecutor?.name || "This engine"} manages its own tools, so these toggles apply to Neo's turns only.`
                      }
                      aria-label="Tools"
                    >
                      <WrenchIcon />
                      <span>{toolTogglesApply ? "Tools" : "Tools (Neo turns only)"}</span>
                    </button>
                    <CompactConversationAction
                      compacting={compacting}
                      disabled={disabled}
                      onCompact={() => {
                        setMenuOpen(false);
                        onCompactConversation?.();
                      }}
                    />
                  </>
                ) : (
                  <>
                    {/* Replies only. An agent run leans on the model's reasoning
                        for tool choice, planning and knowing when it is done, so
                        it is always given it -- and a setting that governed runs
                        from a menu that does not show it would be worse than no
                        setting at all. */}
                    <label className="agent-chip">
                      <span className="agent-chip-label">Effort</span>
                      <select value={effort} onChange={(event) => onEffortChange(event.target.value)}
                        disabled={disabled} aria-label="Select effort"
                        title="High lets a model choose the route and reason before replying. Low keeps the deterministic parts only -- it answers far faster, and it will not decide on its own that a question needs the web.">
                        {/* Default first, as the other chips do. */}
                        <option value="low">Low · faster replies</option>
                        <option value="high">High · more thorough</option>
                      </select>
                    </label>
                    <AttachFilesAction
                      attaching={attaching}
                      disabled={disabled}
                      onPick={() => {
                        setMenuOpen(false);
                        attachInputRef.current?.click();
                      }}
                    />
                    <CompactConversationAction
                      compacting={compacting}
                      disabled={disabled}
                      onCompact={() => {
                        setMenuOpen(false);
                        onCompactConversation?.();
                      }}
                    />
                  </>
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
              /* Ctrl+V is how a screenshot actually arrives. Without this the
                 only way in is the file picker, which means saving the capture
                 to disk first. */
              onPaste={(event) => {
                const pasted = Array.from(event.clipboardData?.items ?? [])
                  .filter((item) => item.kind === "file" && item.type.startsWith("image/"))
                  .map((item) => item.getAsFile())
                  .filter(Boolean);
                if (!pasted.length) return;
                event.preventDefault();
                onAttachFiles?.(pasted);
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
            {/* What the next turn will be, kept away from the button that sends
                it: the choice is made before typing, the send after it. */}
            <div className="chat-mode-switch" role="tablist" aria-label="Interaction mode">
              <button type="button" role="tab" aria-selected={mode === "chatbot"}
                className={mode === "chatbot" ? "active" : ""} onClick={() => onModeChange("chatbot")}>Chat</button>
              <button type="button" role="tab" aria-selected={mode === "agent"}
                className={mode === "agent" ? "active" : ""} onClick={() => onModeChange("agent")}>Agent</button>
            </div>
            <div className="composer-actions">
              {/* One control for both kinds of turn. The toggle opposite decides
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
      <section className="llm-usage-section"><div className="llm-section-heading">Usage history</div>{registry.calls.length === 0 ? <p className="dialog-caption">No routed calls recorded yet.</p> : <div className="llm-usage-list">{registry.calls.slice(0, 20).map((call) => <div key={call.id}><strong>{call.route_name}</strong> · {call.status} · {call.total_tokens ?? "-"} tokens · {call.latency_ms ?? "-"} ms{call.fallback_used ? " · fallback" : ""}{call.error ? ` · ${call.error}` : ""}</div>)}</div>}</section>
      {error && <div className="neo-error">{error}</div>}
      {status && <div className="settings-status">{status}</div>}
    </Modal>
  );
}

/**
 * Whether the gallery may hold the same picture twice.
 *
 * Off (the default) identical bytes resolve to the image already held, so a
 * re-upload records that it was seen again rather than adding a second entry.
 * On, each upload is its own image with its own id, which is what you want when
 * the same file is genuinely a separate occasion.
 */
function BackgroundChatsDialog({ onClose }) {
  const [limit, setLimit] = useState(3);
  const [notify, setNotify] = useState(() => notificationsEnabled());
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    api
      .chatConfig()
      .then((config) => {
        if (!cancelled) setLimit(Number(config.max_concurrent_turns) || 3);
      })
      .catch((requestError) => {
        if (!cancelled) setError(errorMessage(requestError));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function changeLimit(next) {
    setSaving(true);
    setError("");
    const previous = limit;
    setLimit(next);
    try {
      const config = await api.updateChatConfig({ max_concurrent_turns: next });
      setLimit(Number(config.max_concurrent_turns) || next);
    } catch (requestError) {
      setLimit(previous);
      setError(errorMessage(requestError));
    } finally {
      setSaving(false);
    }
  }

  async function toggleNotify(next) {
    setError("");
    if (!next) {
      try {
        window.localStorage.setItem(NOTIFY_STORAGE_KEY, "0");
      } catch {
        // A browser refusing storage still gets the in-app toast.
      }
      setNotify(false);
      return;
    }
    if (typeof Notification === "undefined") {
      setError("This browser cannot show desktop notifications.");
      return;
    }
    // Asked here, from a real click, and nowhere else. Browsers ignore a
    // permission request that does not follow a user gesture, and once a user
    // has denied it the prompt can never be shown again -- so a request fired
    // from a background poll spends the one chance there is and silently fails.
    const permission =
      Notification.permission === "granted"
        ? "granted"
        : await Notification.requestPermission().catch(() => "denied");
    if (permission !== "granted") {
      setError(
        "Your browser is blocking notifications for Neo. Turn them back on in its site " +
          "settings -- Neo cannot ask again once they have been denied.",
      );
      return;
    }
    try {
      window.localStorage.setItem(NOTIFY_STORAGE_KEY, "1");
    } catch {
      // Preference is per device; without storage it simply will not persist.
    }
    setNotify(true);
  }

  return (
    <Modal title="Background chats" onClose={onClose}>
      <p className="dialog-caption">
        Chats keep working when you switch away from them. These decide how many run at once,
        and how you hear about one that finishes while you are elsewhere.
      </p>
      <div className="chat-tools-row">
        <div className="chat-tools-row-info">
          <div className="chat-tools-row-title">
            <strong>Replies at the same time</strong>
          </div>
          <p>
            Anything past this waits its turn and says so in the sidebar. Neo usually talks to one
            model server, so a higher number does not make replies arrive sooner -- it makes more
            of them arrive slowly together.
          </p>
        </div>
        <label className="chat-tools-toggle">
          <select
            value={limit}
            disabled={loading || saving}
            onChange={(event) => changeLimit(Number(event.target.value))}
            aria-label="How many chats may reply at once"
          >
            {[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((value) => (
              <option value={value} key={value}>
                {value}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="chat-tools-row">
        <div className="chat-tools-row-info">
          <div className="chat-tools-row-title">
            <strong>Desktop notifications</strong>
          </div>
          <p>
            Tells you when a chat you are not watching has finished, even if Neo is in another
            tab. Neo always shows a message in the app either way.
          </p>
        </div>
        <label className="chat-tools-toggle">
          <input
            type="checkbox"
            checked={notify}
            onChange={(event) => toggleNotify(event.target.checked)}
            aria-label="Desktop notifications when a background chat finishes"
          />
        </label>
      </div>
      {error && <div className="neo-error">{error}</div>}
    </Modal>
  );
}


function GallerySettingsDialog({ onClose }) {
  const [allowDuplicates, setAllowDuplicates] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    api
      .galleryConfig()
      .then((config) => {
        if (!cancelled) setAllowDuplicates(Boolean(config.allow_duplicates));
      })
      .catch((requestError) => {
        if (!cancelled) setError(errorMessage(requestError));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function toggle(next) {
    setSaving(true);
    setError("");
    // Shown as chosen straight away, and put back if the write fails: a
    // checkbox that lags a round trip reads as an unresponsive one.
    setAllowDuplicates(next);
    try {
      const config = await api.updateGalleryConfig({ allow_duplicates: next });
      setAllowDuplicates(Boolean(config.allow_duplicates));
    } catch (requestError) {
      setAllowDuplicates(!next);
      setError(errorMessage(requestError));
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title="Gallery" onClose={onClose}>
      <p className="dialog-caption">
        Every image you paste into a chat or add here is kept in the gallery, described and
        searchable by what was in it.
      </p>
      <div className="chat-tools-row">
        <div className="chat-tools-row-info">
          <div className="chat-tools-row-title">
            <strong>Allow the same image twice</strong>
          </div>
          <p>
            Off, uploading a picture you have already added keeps the one entry and notes that it
            came up again. On, every upload becomes its own image with its own id, so the same
            file can appear more than once.
          </p>
        </div>
        <label className="chat-tools-toggle">
          <input
            type="checkbox"
            checked={allowDuplicates}
            disabled={loading || saving}
            onChange={(event) => toggle(event.target.checked)}
            aria-label="Allow the same image twice"
          />
        </label>
      </div>
      {error && <div className="neo-error">{error}</div>}
    </Modal>
  );
}

function SettingsDialog({ onOpenAccount, onOpenBackgroundChats, onOpenEngines, onOpenLLMs, onOpenProviderRuntime, onOpenEvaluationHarness, onOpenWorkspaceOrchestration, onOpenContinuity, onOpenRules, onOpenAgents, onOpenBundles, onOpenFiles, onOpenGitHub, onOpenRepos, onOpenContextMemory, onOpenMemoryRetrieval, onOpenReliableWebSearch, onOpenCommandSandbox, onOpenLsp, onOpenMemory, onOpenNotes, onOpenProjects, onOpenResearch, onOpenTasks, onOpenWebSearch, onOpenGallerySettings, onClose }) {
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
        ["Background chats", "How many chats reply at once, and how you're told when one finishes", onOpenBackgroundChats],
      ],
    },
    {
      title: "Capabilities",
      icon: "terminal",
      description: "Connected tools and runtime services.",
      items: [
        ["Engines", "Sign in to Claude Code and Codex so they can run agent turns", onOpenEngines],
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
        ["Gallery", "Images Neo has seen, and whether duplicates are kept", onOpenGallerySettings],
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

  const [activeGroup, setActiveGroup] = useState(groups[0].title);
  const active = groups.find((group) => group.title === activeGroup) || groups[0];

  return (
    <Modal title="Settings" onClose={onClose} className="settings-dialog">
      <div className="set-shell">
        <nav className="set-nav" aria-label="Settings categories">
          {groups.map((group) => (
            <button
              className={`set-nav-item ${group.title === active.title ? "is-active" : ""}`.trim()}
              type="button"
              onClick={() => setActiveGroup(group.title)}
              aria-current={group.title === active.title ? "true" : undefined}
              key={group.title}
            >
              <WorkspaceIcon name={group.icon} />
              <span>{group.title}</span>
            </button>
          ))}
        </nav>
        <div className="set-detail">
          <div className="set-detail-head">
            <h3>{active.title}</h3>
            <p>{active.description}</p>
          </div>
          <div className="set-detail-list">
            {active.items.map(([title, description, onClick]) => (
              <button className="set-row" type="button" onClick={onClick} key={title}>
                <span className="set-row-text">
                  <strong>{title}</strong>
                  <small>{description}</small>
                </span>
                <span className="set-row-arrow" aria-hidden="true">→</span>
              </button>
            ))}
          </div>
        </div>
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
  // What each chat is doing, keyed by chat id, because more than one of them can
  // be working at a time. Everything about a turn that used to be a single piece
  // of state -- which turn, when it started, whether Stop has been pressed --
  // lives in one entry here, so a chat you walked away from keeps its own.
  const [turns, setTurns] = useState(() => new Map());
  const activeChatId = activeChat?.id ?? null;
  const currentTurn = activeChatId === null ? null : turns.get(activeChatId) ?? null;
  // Derived, not stored. "This chat is busy" is exactly "this chat has a turn",
  // and a separate flag beside the map is the thing that drifts out of sync with
  // it -- which is how a background chat finishing used to clear the spinner on
  // the chat you were actually reading.
  const sending = Boolean(currentTurn);
  const activeTurn = currentTurn;
  const stopping = Boolean(currentTurn?.stopping);
  const generationStartedAt = currentTurn?.startedAt ?? null;

  // Callbacks that run from the stream need to read the current turns without
  // being rebuilt every time one changes -- a stale closure here would re-attach
  // to a turn that has already ended.
  const turnsRef = useRef(turns);
  turnsRef.current = turns;

  const setTurnFor = useCallback((chatId, value) => {
    if (!chatId) return;
    setTurns((current) => {
      const next = new Map(current);
      if (value === null) next.delete(chatId);
      else next.set(chatId, { ...(current.get(chatId) ?? {}), ...value });
      return next;
    });
  }, []);

  // One guard per chat, not one for the app. Held in refs, not state, so a second
  // click in the same tick is refused before React can re-render the disabled
  // button -- but a click in a *different* chat was never the double-click this
  // is defending against, and a single shared guard refused it outright.
  const sendGuardsRef = useRef(new Map());
  const guardFor = useCallback((chatId) => {
    const guards = sendGuardsRef.current;
    if (!guards.has(chatId)) guards.set(chatId, createSendGuard());
    return guards.get(chatId);
  }, []);
  // Which reply last triggered an auto-compact, per chat, keyed by that message's
  // id (a global PK) so the same reply never re-fires it while any new one always
  // can.
  const lastAutoCompactedRef = useRef(new Map());

  const [messages, setMessages] = useState([]);
  // The whole loaded chat's token spend, for the Context Window popover -- every
  // message shows the same session-wide figure, regardless of which one you open
  // it from.
  const sessionTokensUsed = useMemo(() => sumTotalTokens(messages), [messages]);
  const [selectedProjectId, setSelectedProjectId] = useState(null);
  const [showNewProjectForm, setShowNewProjectForm] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showAccount, setShowAccount] = useState(false);
  const [chatAttachments, setChatAttachments] = useState([]);
  //: Images travel separately from text attachments: they are enrolled in the
  //: gallery on the way in and then referred to by id, so the same screenshot is
  //: never uploaded twice and stays findable long after the thread scrolls away.
  const [chatImages, setChatImages] = useState([]);
  const [attachingFiles, setAttachingFiles] = useState(false);
  const [attachError, setAttachError] = useState("");
  const [compacting, setCompacting] = useState(false);
  const [showLlmSettings, setShowLlmSettings] = useState(false);
  const [showProviderRuntime, setShowProviderRuntime] = useState(false);
  const [showEvaluationHarness, setShowEvaluationHarness] = useState(false);
  const [showWorkspaceOrchestration, setShowWorkspaceOrchestration] = useState(false);
  const [showContinuity, setShowContinuity] = useState(false);
  const [showWebSearchSettings, setShowWebSearchSettings] = useState(false);
  // Settings > Engines: the one place an external CLI is signed in to. Reachable
  // from Settings and from the composer's engine picker, which no longer offers
  // an engine it cannot actually run.
  const [showEngines, setShowEngines] = useState(false);
  const [showGallerySettings, setShowGallerySettings] = useState(false);
  const [showBackgroundChats, setShowBackgroundChats] = useState(false);
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
  const [elapsedMs, setElapsedMs] = useState(0);
  const [statusError, setStatusError] = useState("");
  const [llms, setLlms] = useState([]);
  const [selectedLlmId, setSelectedLlmId] = useState("");
  const [llmRegistryProviders, setLlmRegistryProviders] = useState([]);
  const [llmRegistryModels, setLlmRegistryModels] = useState([]);
  // Keyed by "<provider type>::<model name>" -- the same pair a chat message
  // stores as provider_name/model_name, so a response can be joined to the
  // context window the registry discovered for that model without a backend
  // change. A model the registry has never seen just resolves to nothing.
  const contextWindowIndex = useMemo(() => {
    const providerTypeById = new Map(llmRegistryProviders.map((provider) => [provider.id, provider.provider_type]));
    const map = new Map();
    for (const model of llmRegistryModels) {
      const providerType = providerTypeById.get(model.provider_id);
      if (!providerType || !model.context_window) {
        continue;
      }
      map.set(`${providerType}::${model.model_name}`, model.context_window);
    }
    return map;
  }, [llmRegistryProviders, llmRegistryModels]);
  const [showResearch, setShowResearch] = useState(false);
  const [showNotes, setShowNotes] = useState(false);
  const [showProjects, setShowProjects] = useState(false);
  const [showTasks, setShowTasks] = useState(false);
  const [showCalendar, setShowCalendar] = useState(false);
  const [showFiles, setShowFiles] = useState(false);
  const [showGallery, setShowGallery] = useState(false);
  const [initialGalleryItemId, setInitialGalleryItemId] = useState(null);
  const [showRepos, setShowRepos] = useState(false);
  /* Closing every workspace in one place. Written out at each call site, one
     of these lists always ends up missing a view -- the gallery was missing
     from four of them, so opening Notes or Research from the sidebar while the
     gallery was up silently did nothing. */
  const closeWorkspaces = useCallback(() => {
    setShowResearch(false);
    setShowNotes(false);
    setShowProjects(false);
    setShowTasks(false);
    setShowCalendar(false);
    setShowFiles(false);
    setShowRepos(false);
    setShowGallery(false);
  }, []);
  const [initialFileId, setInitialFileId] = useState(null);
  const [initialProjectId, setInitialProjectId] = useState(null);
  const [initialNoteId, setInitialNoteId] = useState(null);
  const [initialTaskId, setInitialTaskId] = useState(null);
  const [initialTaskProjectId, setInitialTaskProjectId] = useState(null);
  const [initialCalendarEventId, setInitialCalendarEventId] = useState(null);
  // What the next turn will be. A per-message choice, not a view: the thread
  // stays where it is either way.
  const [chatMode, setChatMode] = useState("chatbot");
  const [agentDefinitions, setAgentDefinitions] = useState([]);
  const [externalAgents, setExternalAgents] = useState([]);
  const [agentRepos, setAgentRepos] = useState([]);
  const [agentBusy, setAgentBusy] = useState(false);
  const [agentPatch, setAgentPatch] = useState("");
  // Which run's diff is currently open, so "View diff" can toggle closed on a
  // second click instead of only ever opening, and so a patch fetched for one
  // run never renders under a different agent turn's DiffView.
  const [agentPatchSessionId, setAgentPatchSessionId] = useState(null);
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
      const [agentData, repoData, externalData] = await Promise.all([
        api.agentDefinitions(false),
        api.reposList({ limit: 100 }).catch(() => ({ repos: [] })),
        // External executors are optional and off by default, so a failure here
        // must leave Agent mode fully usable rather than taking it down.
        api.externalAgents().catch(() => ({ executors: [] })),
      ]);
      setAgentDefinitions(agentData.definitions || []);
      setAgentRepos(repoData.repos || []);
      setExternalAgents(externalData.executors || []);
    } catch (error) {
      setStatusError(`Could not load Agent mode context: ${errorMessage(error)}`);
    }
  }, []);

  useEffect(() => {
    if (chatMode === "agent") loadAgentContext();
  }, [chatMode, loadAgentContext]);

  /**
   * Switch this chat to an engine, and say which one is running turns now.
   *
   * A straight write, because the picker only offers engines that are already
   * usable -- signing in happens in Settings > Engines, before an engine ever
   * reaches this menu. The confirmation stays: the only other visible change is
   * a collapsed box of caveats, and on its own that reads like a failure.
   */
  const handleEngineSelected = useCallback(
    (id) => {
      updateChatAgentSettings({ executor: id });
      if (id === "neo") {
        setChatAgentMessage("");
        return;
      }
      const name = externalAgents.find((agent) => agent.id === id)?.name || "That engine";
      setChatAgentMessage(`Agent turns in this chat run on ${name}.`);
      window.setTimeout(() => setChatAgentMessage(""), 6000);
    },
    [externalAgents, updateChatAgentSettings],
  );

  // There is deliberately no poll here. The badges once needed an 8-second one
  // because nothing else told the sidebar a run had ended; the profile-wide tail
  // now carries every chat's turn.queued, run.started and terminals, the badge
  // reads live stream state before the stored value, and `handleTurnEnd`
  // refreshes the sidebar when a turn finishes. A timer beside all of that would
  // be a second source of truth for the same thing, refetching for the whole
  // duration of every background turn to learn what the stream already said.

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
    const runningTurn = thread.messages.find(
      (message) => message.agent && ACTIVE_RUN_STATUSES.has(message.agent.session?.status),
    );
    // Re-attaching is only for a turn this browser is not already watching. The
    // live stream is the fresher source, and it has been running the whole time
    // -- overwriting its entry with a REST snapshot would rewind the timer on a
    // chat that never stopped.
    const known = turnsRef.current.get(thread.chat.id);
    if (runningTurn) {
      if (!known) {
        setTurnFor(thread.chat.id, {
          kind: "agent",
          sessionId: runningTurn.agent.session.id,
          startedAt: parseNeoTimestamp(runningTurn.agent.session.started_at) || Date.now(),
        });
      }
      return thread;
    }
    const generation = await api.activeChatGeneration(thread.chat.id).catch(() => null);
    if (generation) {
      if (!known) {
        setTurnFor(thread.chat.id, {
          kind: "chat",
          generationId: generation.id,
          startedAt:
            parseNeoTimestamp(generation.started_at || generation.created_at) || Date.now(),
        });
      }
    } else if (known) {
      setTurnFor(thread.chat.id, null);
    }
    return thread;
  }, [setTurnFor]);

  // A proposal's resolution is server state now, so the transcript only needs
  // the stamped metadata swapped in. Patching the one message keeps the scroll
  // position where a reload of the whole thread would not, and the next
  // loadChat reads back exactly the same thing from the database.
  const handleProposalResolved = useCallback((messageId, proposal) => {
    setMessages((current) =>
      current.map((message) =>
        message.id === messageId
          ? { ...message, metadata: { ...message.metadata, calendar_proposal: proposal } }
          : message,
      ),
    );
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
        // An empty chat is reused rather than replaced, so asking for a new one
        // twice can land on the thread already in the address bar. Pushing there
        // would stack identical entries and turn one Back press into several.
        const current = parsePermalink();
        const samePlace =
          (current?.type === "chat" || current?.type === "projectChat") && current.id === chat.id;
        updatePermalink(chatPermalink(chat.id, chat.project_id), {
          replace: options.history === "replace" || samePlace,
        });
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

  // Finished background turns waiting to be acknowledged. Keyed by the event's
  // sequence number, which is unique across the whole profile.
  const [turnNotices, setTurnNotices] = useState([]);
  const pushTurnNotice = useCallback((chatId, event) => {
    setTurnNotices((current) => {
      if (current.some((notice) => notice.id === event?.seq)) return current;
      return [
        ...current,
        {
          id: event?.seq ?? `${chatId}-${Date.now()}`,
          chatId,
          outcome: event?.type ?? "run.completed",
        },
      ];
    });
  }, []);
  const dismissTurnNotice = useCallback(
    (id) => setTurnNotices((current) => current.filter((notice) => notice.id !== id)),
    [],
  );

  // The stream is opened before the handler exists, and reaches it through a ref.
  // The two genuinely are circular -- the handler clears stream state, the stream
  // calls the handler -- and naming `clearStream` in the handler's dependency
  // array before this line has run is a crash on the first render, not a warning.
  const turnEndRef = useRef(null);
  const { streams, clear: clearStream } = useChatStreams({
    onTurnEnd: (chatId, event) => turnEndRef.current?.(chatId, event),
  });
  const live = (activeChatId && streams.get(activeChatId)) || IDLE;

  /**
   * Fold a finished turn into the transcript, whichever chat it belonged to.
   *
   * A turn ends by writing a message row, so the thread is reloaded and the live
   * state stands down rather than lingering as a second copy of what is now on
   * the page. The chat comes from the event rather than from what is on screen,
   * because the whole point is that the turn may have finished somewhere the
   * user is not looking -- reading the visible chat here would have cleared the
   * spinner on whatever they had switched to instead.
   */
  const maybeAutoCompact = useCallback(
    async (chatId, chatMessages) => {
      // Same math ContextWindowIndicator uses, so "100%" here matches what the
      // ring shows -- checked once the turn has actually finished, never mid-stream.
      const latest = [...chatMessages]
        .reverse()
        .find((message) => Number.isFinite(message.total_tokens));
      if (!latest || lastAutoCompactedRef.current.get(chatId) === latest.id) return;
      const used = sumTotalTokens(chatMessages);
      const windowSize = resolveContextWindow(latest, contextWindowIndex);
      const pct = windowSize ? (used / windowSize) * 100 : null;
      if (pct === null || pct < 100) return;
      lastAutoCompactedRef.current.set(chatId, latest.id);
      try {
        const result = await api.compactChat(chatId);
        if (result.compacted_message_count > 0 && streamChatIdRef.current === chatId) {
          await loadChat(chatId, { history: "none" });
        }
      } catch {
        // Silent: a background convenience, not a user-initiated action --
        // the percentage simply stays elevated and the manual button remains.
      }
    },
    [contextWindowIndex, loadChat],
  );

  const handleTurnEnd = useCallback(
    async (chatId, event) => {
      if (!chatId) return;
      const visible = chatId === streamChatIdRef.current;
      if (event?.type === "run.failed" && event.error && visible) setStatusError(event.error);
      try {
        // `loadChat` switches what is on screen, so it is only right for the chat
        // already there. A background chat is fetched instead -- which costs one
        // request per finished background turn, and buys a chat that crosses its
        // context limit while you are elsewhere still being compacted rather than
        // waiting until you next open it.
        const thread = visible
          ? await loadChat(chatId, { history: "none" })
          : await api.getChat(chatId);
        await maybeAutoCompact(chatId, thread.messages || []);
      } catch (error) {
        if (visible) setStatusError(`Could not reload the chat: ${errorMessage(error)}`);
      }
      setTurnFor(chatId, null);
      guardFor(chatId).release();
      // Dropped only now: until the transcript above has been reloaded, this
      // buffer is the only copy of what the turn said, and clearing it earlier
      // would blank the pane for the moment in between.
      clearStream(chatId);
      if (
        shouldNotify({
          chatId,
          visibleChatId: visibleChatIdRef.current,
          hidden: document.hidden,
        })
      ) {
        pushTurnNotice(chatId, event);
      }
      refreshSidebar().catch(() => {});
    },
    [
      loadChat,
      refreshSidebar,
      maybeAutoCompact,
      setTurnFor,
      guardFor,
      clearStream,
      pushTurnNotice,
    ],
  );
  turnEndRef.current = handleTurnEnd;

  // Chats that finished while the user was elsewhere, so the badge can say so
  // until they look. Cleared when the chat is opened -- "done" is news, and news
  // that has been read stops being a badge.
  const [finishedChatIds, setFinishedChatIds] = useState(() => new Set());
  useEffect(() => {
    if (!turnNotices.length) return;
    setFinishedChatIds((current) => {
      const next = new Set(current);
      for (const notice of turnNotices) {
        if (notice.outcome !== "run.cancelled") next.add(notice.chatId);
      }
      return next;
    });
  }, [turnNotices]);
  useEffect(() => {
    if (activeChatId === null) return;
    setFinishedChatIds((current) => {
      if (!current.has(activeChatId)) return current;
      const next = new Set(current);
      next.delete(activeChatId);
      return next;
    });
  }, [activeChatId]);

  // The badge reads from the live stream first and the sidebar payload second.
  // The stream is up to eight seconds fresher than a refetch, which is the
  // difference between a badge that tracks the work and one that lags it.
  const chatTitlesById = useMemo(() => {
    const titles = new Map();
    for (const chat of sidebar.chats || []) titles.set(chat.id, chat.title);
    for (const project of sidebar.projects || []) {
      for (const chat of project.chats || []) titles.set(chat.id, chat.title);
    }
    return titles;
  }, [sidebar]);

  const statusFor = useCallback(
    (chat) => {
      const liveState = streams.get(chat.id);
      if (liveState?.sessionStatus) return liveState.sessionStatus;
      if (liveState?.kind) return "running";
      if (turns.has(chat.id)) return "running";
      const stored = chat.turn_status || chat.agent_status;
      if (stored) return stored;
      return finishedChatIds.has(chat.id) ? "done" : null;
    },
    [streams, turns, finishedChatIds],
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
        try {
          const [providers, models] = await Promise.all([api.llmProviders(), api.llmModels()]);
          setLlmRegistryProviders(providers.providers || []);
          setLlmRegistryModels(models.models || []);
        } catch {
          // Context-window lookups just degrade to "unknown" per message -- never
          // worth surfacing an error over, let alone blocking the rest of bootstrap.
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
    visibleChatIdRef.current = showProjects || showTasks || showCalendar || showNotes || showResearch ? null : activeChat?.id ?? null;
  }, [activeChat?.id, showCalendar, showNotes, showProjects, showResearch, showTasks]);

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
      setTurnFor(activeChat.id, {
        kind: "chat",
        generationId: result.generation.id,
        startedAt: parseNeoTimestamp(result.generation.created_at) || Date.now(),
        stopping: false,
      });
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
  async function sendPrompt(prompt, turnMode = "chat", imageIds = []) {
    // `sending` catches a send started by another flow (a rerun, or a generation resumed
    // after reload). It cannot catch a second click in the same tick, because it only
    // becomes true on the next render -- that window is what the guard closes.
    if (!prompt || sending) {
      return;
    }
    // A chat that does not exist yet cannot have a guard of its own, and two
    // clicks in that state must still collapse into one chat -- which is what
    // `createActiveChat`'s in-flight promise already guarantees. For an existing
    // chat the guard is per chat, so a send here never refuses a send elsewhere.
    const guard = guardFor(activeChat?.id ?? NEW_CHAT_GUARD_KEY);
    const requestId = guard.begin();
    if (requestId === null) {
      return;
    }

    setStatusError("");
    setChatAgentMessage("");
    setElapsedMs(0);
    const pendingId = `pending-${Date.now()}`;
    const optimisticMessage = {
      id: pendingId,
      chat_id: activeChat?.id ?? null,
      role: "user",
      content: prompt,
      created_at: new Date().toISOString(),
      metadata: imageIds.length ? { image_ids: imageIds } : undefined,
    };
    setMessages((current) => [...current, optimisticMessage]);

    let chatId = activeChat?.id ?? null;
    try {
      const chat = activeChat ?? (await createActiveChat(selectedProjectId, { resetMessages: false }));
      chatId = chat.id;
      // The turn is recorded against the chat before the request is even sent, so
      // Stop has something to stop during the gap between the POST and the first
      // event -- which is exactly when a user reaches for it.
      setTurnFor(chatId, { kind: "chat", startedAt: Date.now(), stopping: false });
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
          executor: chat.executor ?? null,
          effort: chat.effort ?? null,
          imageIds,
        },
      );
      if (result.agent_session_id) {
        setTurnFor(chatId, { kind: "agent", sessionId: result.agent_session_id });
        // The run wrote a row to hold its place in the transcript, and the trace
        // draws into that row -- so the thread is reloaded rather than waiting
        // for the first event to imply a turn that is already there.
        if (streamChatIdRef.current === chatId) await loadChat(chatId, { history: "none" });
        refreshSidebar().catch(() => {});
      } else {
        setTurnFor(chatId, { kind: "chat", generationId: result.generation.id });
      }
    } catch (error) {
      // The user may have switched chats during the round trip, and marking the
      // bubble failed reaches into whatever transcript is loaded now -- so the
      // transcript edits only apply if this is still the chat on screen. The turn
      // state is keyed by chat and is always safe to clear.
      if (streamChatIdRef.current === chatId || chatId === null) {
        setMessages((current) =>
          current.map((message) =>
            message.id === pendingId ? { ...message, failed: true } : message,
          ),
        );
        setComposerValue(prompt);
        setStatusError(`${errorMessage(error)}. Your message was not sent, but it was kept.`);
      }
      setTurnFor(chatId, null);
    } finally {
      // A send that failed has no turn to end, so nothing else will release this.
      // A send that succeeded is released again by `handleTurnEnd`; releasing a
      // guard twice is harmless, leaving one held forever is not.
      guard.release();
    }
  }

  async function handleAttachFiles(files) {
    setAttachingFiles(true);
    setAttachError("");
    try {
      const uploaded = [];
      const enrolled = [];
      for (const file of files) {
        // An image goes to the gallery, where it is described, embedded and
        // remembered. Everything else stays a plain workspace file.
        if (file.type?.startsWith("image/")) {
          const data = await api.uploadGalleryImage(file, {
            origin: "paste",
            chatId: activeChat?.id ?? null,
          });
          enrolled.push(data.item);
        } else {
          const data = await api.uploadFile(file);
          uploaded.push(data.file);
        }
      }
      if (uploaded.length) setChatAttachments((current) => [...current, ...uploaded]);
      if (enrolled.length) {
        setChatImages((current) => {
          const seen = new Set(current.map((item) => item.id));
          return [...current, ...enrolled.filter((item) => !seen.has(item.id))];
        });
      }
    } catch (error) {
      setAttachError(errorMessage(error));
    } finally {
      setAttachingFiles(false);
    }
  }

  function handleRemoveAttachment(fileId) {
    setChatAttachments((current) => current.filter((file) => file.id !== fileId));
    setChatImages((current) => current.filter((item) => item.id !== fileId));
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
    setTurnFor(activeChat.id, { stopping: true });
    try {
      if (activeTurn.kind === "agent") await api.cancelAgentSession(activeTurn.sessionId);
      else await api.cancelChatGeneration(activeChat.id, activeTurn.generationId);
      // The tail sees the cancellation and tears the live state down.
    } catch (error) {
      setTurnFor(activeChat.id, { stopping: false });
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
      setChatImages([]);
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
    const imageIds = chatImages.map((item) => item.id);
    setComposerValue("");
    setChatAttachments([]);
    setChatImages([]);
    setAttachError("");
    await sendPrompt(outgoing, chatMode === "agent" ? "agent" : "chat", imageIds);
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
    if (deliverMode === "patch" && agentPatchSessionId === run.session.id) {
      // The diff for this run is already open -- this click is "Hide diff".
      setAgentPatch("");
      setAgentPatchSessionId(null);
      return;
    }
    setAgentBusy(true);
    try {
      const result = await api.deliverAgentChanges(run.session.id, { mode: deliverMode });
      if (deliverMode === "patch") {
        setAgentPatch(result.patch || "(no changes)");
        setAgentPatchSessionId(run.session.id);
      } else {
        setChatAgentMessage(`Wrote ${result.written?.length ?? 0} file(s) into your repository.`);
      }
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
      setAgentPatchSessionId(null);
      setChatAgentMessage(
        skipped.length
          ? `Undid ${reversed} file(s). Left alone: ${skipped
            .map((item) => item.path)
            .join(", ")}, changed after the run finished.`
          : `Undid ${reversed} file(s).`,
      );
      await loadChat(activeChat.id, { history: "none" });
    } catch (error) {
      setChatAgentMessage(errorMessage(error));
    } finally {
      setAgentBusy(false);
    }
  }

  async function handleAgentFork(message) {
    if (!message?.id || !activeChat?.id) return;
    setAgentBusy(true);
    try {
      const forked = await api.forkChat(activeChat.id, message.id);
      await refreshSidebar();
      await loadChat(forked.id);
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

  async function handleCompactConversation() {
    if (!activeChat?.id || compacting) return;
    setStatusError("");
    setCompacting(true);
    try {
      const result = await api.compactChat(activeChat.id);
      await loadChat(activeChat.id, { history: "none" });
      if (result.compacted_message_count === 0) {
        setChatAgentMessage("Nothing to compact yet.");
        window.setTimeout(() => setChatAgentMessage(""), 3000);
      }
    } catch (error) {
      setStatusError(`Could not compact this conversation: ${errorMessage(error)}`);
    } finally {
      setCompacting(false);
    }
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
              : showCalendar ? "calendar"
                : showFiles ? "files"
                  : showGallery ? "gallery"
                    : showRepos ? "repos"
                      : "chat";
  return (
    <div className={`neo-app${sidebarCollapsed ? " sidebar-collapsed" : ""}`}>
      <Sidebar
        sidebar={sidebar}
        activeChatId={activeChat?.id ?? null}
        statusFor={statusFor}
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
        onOpenChatHome={closeWorkspaces}
        onOpenMemory={() => setShowMemory(true)}
        onOpenResearch={() => { closeWorkspaces(); setShowResearch(true); }}
        onOpenNotes={() => { closeWorkspaces(); setInitialNoteId(null); setShowNotes(true); }}
        onOpenTasks={() => {
          closeWorkspaces(); setInitialTaskId(null); setInitialTaskProjectId(null); setShowTasks(true);
        }}
        onOpenCalendar={() => { closeWorkspaces(); setInitialCalendarEventId(null); setShowCalendar(true); }}
        onOpenGallery={() => { closeWorkspaces(); setInitialGalleryItemId(null); setShowGallery(true); }}
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
      ) : showCalendar ? (
        <Calendar
          initialEventId={initialCalendarEventId}
          onBack={() => { setShowCalendar(false); setInitialCalendarEventId(null); }}
        />
      ) : showGallery ? (
        <Gallery
          initialItemId={initialGalleryItemId}
          onBack={() => { setShowGallery(false); setInitialGalleryItemId(null); }}
          onOpenChat={(chatId) => {
            setShowGallery(false);
            setInitialGalleryItemId(null);
            loadChat(chatId).catch(() => {});
          }}
        />
      ) : showNotes ? (
        <Notes
          initialNoteId={initialNoteId}
          onBack={() => {
            setShowNotes(false);
            setInitialNoteId(null);
          }}
          onOpenTask={(taskId) => {
            closeWorkspaces(); setInitialTaskId(taskId); setInitialTaskProjectId(null); setShowTasks(true);
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
            closeWorkspaces(); setInitialNoteId(noteId); setShowNotes(true);
          }}
        />
      ) : (
      <main className={`neo-main ${chatMode === "agent" ? "agent-chat-mode" : ""}`}>
        <header className="neo-view-header">
          {/* The same words the backend names a new chat with. On a refresh the
              chat is briefly not loaded yet, so this placeholder renders first
              and the real title replaces it -- and when the two disagreed that
              swap was visible as a flash from "New conversation" to "New chat". */}
          <span>{activeChat?.title || "New chat"}</span>
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
              contextWindowIndex={contextWindowIndex}
              sessionTokensUsed={sessionTokensUsed}
              editingMessageId={editingMessageId}
              editingValue={editingValue}
              onCancelEdit={() => {
                setEditingMessageId(null);
                setEditingValue("");
              }}
              onCopy={copyText}
              onEdit={handleEditMessage}
              onOpenGalleryItem={(itemId) => {
                setInitialGalleryItemId(itemId);
                setShowGallery(true);
              }}
              onRerun={(prompt) => sendPrompt(prompt)}
              onSaveEdit={handleSaveEditedMessage}
              onSetEditingValue={setEditingValue}
              onToggleThinking={(messageId) =>
                setOpenThinkingMessageId((current) => (current === messageId ? null : messageId))
              }
              thinkingOpen={openThinkingMessageId === message.id}
              onOpenCalendar={() => {
                setInitialCalendarEventId(null);
                setShowResearch(false); setShowNotes(false); setShowProjects(false); setShowTasks(false); setShowFiles(false); setShowRepos(false);
                setShowCalendar(true);
              }}
              onProposalResolved={handleProposalResolved}
              agentRun={agentRuns[message.id]}
              agentEntries={agentRuns[message.id]?.liveEntries}
              agentBusy={agentBusy}
              agentPatch={agentPatch}
              agentPatchSessionId={agentPatchSessionId}
              onAgentDecide={handleAgentDecide}
              onAgentDeliver={handleAgentDeliver}
              onAgentUndo={handleAgentUndo}
              onAgentFork={handleAgentFork}
              onCloseAgentPatch={() => {
                setAgentPatch("");
                setAgentPatchSessionId(null);
              }}
            />
          ))}

          {streamingAssistant && (
            <PendingAssistantMessage generation={streamingAssistant} elapsedMs={elapsedMs} />
          )}

          {statusError && <div className="neo-error">{statusError}</div>}
        </section>

        <ChatComposer
          /* Stop is offered whenever something is running, full stop -- a run
             being steered is still a run someone may want to cancel outright. */
          generating={sending && Boolean(activeTurn)}
          onStop={handleStopGeneration}
          stopping={stopping}
          steering={Boolean(steeringSessionId)}
          attachments={chatAttachments}
          images={chatImages}
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
          executor={activeChat?.executor || "neo"}
          onExecutorChange={handleEngineSelected}
          externalAgents={externalAgents}
          onOpenEngineSettings={() => setShowEngines(true)}
          effort={activeChat?.effort || "low"}
          onEffortChange={(next) => updateChatAgentSettings({ effort: next })}
          onOpenFolder={handleOpenFolder}
          folderAttaching={false}
          onOpenToolsPanel={() => setShowChatTools(true)}
          agentMessage={chatAgentMessage}
          onCompactConversation={handleCompactConversation}
          compacting={compacting}
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
          onOpenBackgroundChats={() => { setShowSettings(false); setShowBackgroundChats(true); }}
          onOpenEngines={() => { setShowSettings(false); setShowEngines(true); }}
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
          onOpenGallerySettings={() => { setShowSettings(false); setShowGallerySettings(true); }}
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

      {showGallerySettings && (
        <GallerySettingsDialog onClose={() => setShowGallerySettings(false)} />
      )}

      {showEngines && (
        <ExternalAgents
          onClose={() => setShowEngines(false)}
          /* Signing in here has to reach the composer's picker without a
             reload: its list was fetched when Agent mode was entered, and an
             engine that appears only on the next visit reads as one that did
             not connect. */
          onChanged={loadAgentContext}
        />
      )}

      {showBackgroundChats && (
        <BackgroundChatsDialog onClose={() => setShowBackgroundChats(false)} />
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
      <BackgroundTurnToast
        notices={turnNotices}
        chatTitles={chatTitlesById}
        onOpen={handleOpenChat}
        onDismiss={dismissTurnNotice}
      />
      <ReminderToast />
    </div>
  );
}

// Every key here holds state that belongs to one profile. They live in
// localStorage, which is scoped to the origin rather than to the profile, so a
// profile transition has to drop them explicitly, otherwise the next profile
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
