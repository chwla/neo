from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from html import escape

import requests
import streamlit as st
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models import Project
from app.models.enums import CandidateStatus, GoalStatus, ProjectStatus
from app.repositories.memory_store import MemoryStore
from app.schemas.memory_objects import MemoryCandidateRead
from app.services.chat import NeoChatService
from app.services.ollama_client import OllamaClient
from app.services.review import MemoryReviewRequest, MemoryReviewService

MODEL_NAME = "qwen3:8b-q4_K_M"
OLLAMA_URL = "http://127.0.0.1:11434"


@contextmanager
def session_scope() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def inject_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
            --neo-green: #39ff14;
            --neo-black: #030603;
            --neo-panel: #071007;
            --neo-panel-2: #0b180b;
            --neo-text: #eaffea;
            --neo-muted: #89ad89;
            --neo-line: rgba(57, 255, 20, .32);
        }
        .stApp {
            background:
                linear-gradient(rgba(57,255,20,.035) 1px, transparent 1px),
                linear-gradient(90deg, rgba(57,255,20,.035) 1px, transparent 1px),
                radial-gradient(circle at 20% 10%, rgba(57,255,20,.12), transparent 26%),
                #020402;
            background-size: 36px 36px, 36px 36px, auto;
            color: var(--neo-text);
        }
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #020402, #071407);
            border-right: 1px solid var(--neo-line);
        }
        [data-testid="stHeader"] {
            background: rgba(2, 4, 2, .72);
            border-bottom: 1px solid rgba(57,255,20,.12);
        }
        .neo-shell {
            max-width: 980px;
            margin: 0 auto;
        }
        .neo-title {
            font-size: 30px;
            font-weight: 800;
            letter-spacing: 0;
            color: var(--neo-green);
            text-shadow: 0 0 18px rgba(57,255,20,.75);
            margin-bottom: 0;
        }
        .neo-subtitle {
            color: var(--neo-muted);
            margin-top: 2px;
            margin-bottom: 24px;
        }
        [data-testid="stChatMessage"] {
            background: rgba(7, 16, 7, .82);
            border: 1px solid rgba(57,255,20,.18);
            border-radius: 8px;
            box-shadow: 0 0 22px rgba(57,255,20,.06);
        }
        [data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) {
            background: rgba(13, 28, 13, .92);
            border-color: rgba(57,255,20,.32);
        }
        .stChatInput textarea {
            background: #061006 !important;
            color: var(--neo-text) !important;
            border: 1px solid var(--neo-line) !important;
            border-radius: 8px !important;
            box-shadow: 0 0 18px rgba(57,255,20,.10);
            padding: 18px 58px 18px 18px !important;
            min-height: 56px !important;
        }
        [data-testid="stChatInput"] {
            padding-top: 18px;
        }
        [data-testid="stChatInputSubmitButton"] {
            margin-right: 8px;
        }
        .stButton > button, .stDownloadButton > button {
            background: #061006;
            color: var(--neo-green);
            border: 1px solid var(--neo-line);
            border-radius: 7px;
        }
        .stButton > button:hover {
            border-color: var(--neo-green);
            box-shadow: 0 0 16px rgba(57,255,20,.25);
            color: #caffc2;
        }
        .stTabs [data-baseweb="tab-list"] {
            border-bottom: 1px solid var(--neo-line);
        }
        .stTabs [data-baseweb="tab"] {
            color: var(--neo-muted);
        }
        .stTabs [aria-selected="true"] {
            color: var(--neo-green);
        }
        .neo-status, .neo-card {
            border: 1px solid var(--neo-line);
            background: rgba(7, 16, 7, .88);
            border-radius: 8px;
            padding: 10px 12px;
            margin: 8px 0 14px;
        }
        .neo-pill {
            display: inline-block;
            padding: 2px 8px;
            border: 1px solid var(--neo-line);
            border-radius: 999px;
            color: var(--neo-green);
            font-size: 12px;
            margin-right: 6px;
        }
        .sidebar-title {
            color: var(--neo-green);
            font-size: 18px;
            font-weight: 700;
            margin: 4px 0 16px;
        }
        .sidebar-section {
            color: var(--neo-muted);
            font-size: 14px;
            letter-spacing: 0;
            text-transform: none;
            margin: 18px 0 8px;
        }
        [data-testid="stSidebar"] .stButton > button {
            min-height: 34px;
            font-size: 16px;
        }
        [data-testid="stSidebar"] a,
        [data-testid="stSidebar"] a:link,
        [data-testid="stSidebar"] a:visited,
        [data-testid="stSidebar"] a:hover,
        [data-testid="stSidebar"] a:active {
            color: var(--neo-green) !important;
            text-decoration: none !important;
        }
        [data-testid="stSidebar"] summary {
            color: var(--neo-text);
            border-radius: 8px;
            padding: 6px 8px;
        }
        [data-testid="stSidebar"] summary:hover {
            background: rgba(57,255,20,.08);
            color: var(--neo-green);
        }
        .sidebar-project {
            display: flex;
            align-items: center;
            gap: 8px;
            color: var(--neo-text);
            font-size: 14px;
            font-weight: 600;
            padding: 6px 8px;
            margin-top: 8px;
            border-radius: 7px;
            text-decoration: none;
        }
        .sidebar-project.active {
            background: rgba(57,255,20,.12);
            color: var(--neo-green);
        }
        .sidebar-project:hover {
            background: rgba(57,255,20,.08);
            color: var(--neo-green);
        }
        .sidebar-folder {
            color: var(--neo-green);
            font-size: 15px;
        }
        .project-new-chat button {
            justify-content: flex-start !important;
            min-height: 30px !important;
            margin-left: 24px;
            color: var(--neo-green) !important;
            border-color: transparent !important;
            background: transparent !important;
        }
        .chat-item {
            min-height: 32px;
            display: flex;
            align-items: stretch;
            border: 0;
            border-radius: 7px;
            background: transparent;
            margin: 3px 0 3px 24px;
            overflow: hidden;
            box-shadow: none;
        }
        .chat-item.active {
            background: rgba(57,255,20,.12);
            box-shadow: none;
        }
        .chat-item:hover {
            background: rgba(57,255,20,.08);
        }
        .chat-item-title {
            flex: 1;
            display: flex;
            align-items: center;
            justify-content: flex-start;
            padding: 7px 8px;
            color: var(--neo-green);
            text-decoration: none;
            font-size: 14px;
            font-weight: 500;
            min-width: 0;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .chat-item a,
        .chat-item a:link,
        .chat-item a:visited,
        .chat-item a:hover,
        .chat-item a:active {
            color: var(--neo-green) !important;
            text-decoration: none !important;
        }
        .chat-item-delete {
            width: 28px;
            flex: 0 0 28px;
            display: flex;
            align-items: center;
            justify-content: center;
            border-left: 0;
            color: var(--neo-green);
            font-size: 14px;
            font-weight: 700;
            text-decoration: none;
            opacity: 0;
            visibility: hidden;
            pointer-events: none;
            transition: opacity .12s ease, visibility .12s ease, background .12s ease;
        }
        .chat-item:hover .chat-item-delete,
        .chat-item:focus-within .chat-item-delete {
            opacity: 1;
            visibility: visible;
            pointer-events: auto;
        }
        .chat-item-title:hover,
        .chat-item-delete:hover {
            color: #caffc2;
            background: rgba(57,255,20,.08);
        }
        .project-folder {
            border: 1px solid rgba(57,255,20,.22);
            border-radius: 7px;
            background: transparent;
            margin: 3px 0 3px 24px;
            overflow: hidden;
        }
        .project-folder[open] {
            background: rgba(57,255,20,.025);
        }
        .project-folder summary {
            list-style: none;
            min-height: 32px;
            display: flex;
            align-items: center;
            gap: 8px;
            cursor: pointer;
            padding: 0 0 0 8px !important;
        }
        .project-folder summary::-webkit-details-marker {
            display: none;
        }
        .project-folder-icon {
            width: 16px;
            height: 16px;
            flex: 0 0 16px;
            color: var(--neo-green);
            filter: drop-shadow(0 0 5px rgba(57,255,20,.4));
        }
        .project-folder-title {
            flex: 1;
            color: var(--neo-text);
            font-size: 14px;
            font-weight: 500;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .project-folder-delete {
            width: 28px;
            flex: 0 0 28px;
            align-self: stretch;
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--neo-green) !important;
            text-decoration: none !important;
            font-size: 14px;
            font-weight: 700;
            opacity: 0;
            visibility: hidden;
            pointer-events: none;
            transition: opacity .12s ease, visibility .12s ease, background .12s ease;
        }
        .project-folder:hover .project-folder-delete,
        .project-folder:focus-within .project-folder-delete {
            opacity: 1;
            visibility: visible;
            pointer-events: auto;
        }
        .project-folder-delete:hover {
            background: rgba(57,255,20,.08);
        }
        .project-folder-new-chat {
            display: block;
            margin: 2px 0 4px 32px;
            padding: 6px 8px;
            border-radius: 7px;
            color: var(--neo-green) !important;
            text-decoration: none !important;
            font-size: 14px;
        }
        .project-folder-new-chat:hover {
            background: rgba(57,255,20,.08);
        }
        .project-chat-link {
            display: block;
            margin: 2px 8px 4px 32px;
            padding: 7px 8px;
            border-radius: 7px;
            color: var(--neo-text) !important;
            text-decoration: none !important;
            font-size: 15px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .project-chat-link.active,
        .project-chat-link:hover {
            color: var(--neo-green) !important;
            background: rgba(57,255,20,.08);
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def initialize_state() -> None:
    st.session_state.setdefault("active_chat_id", None)
    st.session_state.setdefault("selected_project_id", None)
    st.session_state.setdefault("show_memory", False)
    st.session_state.setdefault("show_new_project_form", False)
    st.session_state.setdefault("pending_delete_chat_id", None)
    st.session_state.setdefault("pending_delete_project_id", None)


def query_param_value(name: str) -> str | None:
    value = st.query_params.get(name)
    if isinstance(value, list):
        return value[0] if value else None
    return value


def handle_sidebar_query_actions() -> None:
    open_chat_id = query_param_value("open_chat")
    request_delete_chat_id = query_param_value("request_delete_chat")
    request_delete_project_id = query_param_value("request_delete_project")
    new_project_chat_id = query_param_value("new_project_chat")
    select_project_id = query_param_value("select_project")
    if open_chat_id:
        open_chat(int(open_chat_id))
        st.query_params.clear()
        st.rerun()
    if request_delete_chat_id:
        st.session_state.pending_delete_chat_id = int(request_delete_chat_id)
        st.session_state.pending_delete_project_id = None
        st.query_params.clear()
        st.rerun()
    if request_delete_project_id:
        st.session_state.pending_delete_project_id = int(request_delete_project_id)
        st.session_state.pending_delete_chat_id = None
        st.query_params.clear()
        st.rerun()
    if new_project_chat_id:
        create_new_chat(int(new_project_chat_id))
        st.query_params.clear()
        st.rerun()
    if select_project_id:
        st.session_state.selected_project_id = int(select_project_id)
        st.query_params.clear()
        st.rerun()


def ensure_active_chat() -> None:
    with session_scope() as db:
        store = MemoryStore(db)
        chat_id = st.session_state.active_chat_id
        if chat_id and store.get_chat(chat_id):
            return
        chat = store.create_chat(project_id=st.session_state.selected_project_id)
        db.commit()
        st.session_state.active_chat_id = chat.id


def create_new_chat(project_id: int | None = None) -> None:
    with session_scope() as db:
        chat = MemoryStore(db).create_chat(project_id=project_id)
        db.commit()
        st.session_state.active_chat_id = chat.id
        st.session_state.selected_project_id = project_id


def open_chat(chat_id: int) -> None:
    with session_scope() as db:
        chat = MemoryStore(db).get_chat(chat_id)
        st.session_state.active_chat_id = chat_id
        st.session_state.selected_project_id = chat.project_id if chat else None


def clear_active_chat_if_needed(chat_id: int) -> None:
    if st.session_state.active_chat_id == chat_id:
        st.session_state.active_chat_id = None


def delete_chat(chat_id: int) -> None:
    with session_scope() as db:
        MemoryStore(db).delete_chat(chat_id)
        db.commit()
    clear_active_chat_if_needed(chat_id)


def clear_active_project_if_needed(project_id: int) -> None:
    if st.session_state.selected_project_id == project_id:
        st.session_state.selected_project_id = None
    with session_scope() as db:
        active_chat_id = st.session_state.active_chat_id
        if active_chat_id is None:
            return
        chat = MemoryStore(db).get_chat(active_chat_id)
        if chat and chat.project_id == project_id:
            st.session_state.active_chat_id = None


def delete_project(project_id: int) -> None:
    with session_scope() as db:
        store = MemoryStore(db)
        if hasattr(store, "delete_project"):
            store.delete_project(project_id)
        else:
            project = db.get(Project, project_id)
            if project is not None:
                for chat in list(project.chats):
                    db.delete(chat)
                db.delete(project)
                db.flush()
        db.commit()
    clear_active_project_if_needed(project_id)


def create_project(name: str) -> None:
    cleaned = " ".join(name.split())
    if not cleaned:
        return
    with session_scope() as db:
        project = MemoryStore(db).create_project(cleaned)
        db.commit()
        st.session_state.selected_project_id = project.id


def chat_row_html(label: str, chat_id: int, active: bool) -> str:
    display = label
    active_class = " active" if active else ""
    safe_label = escape(display)
    return f"""
        <div class="chat-item{active_class}" data-chat-id="{chat_id}">
            <a class="chat-item-title" href="?open_chat={chat_id}" target="_self">
                {safe_label}
            </a>
            <a class="chat-item-delete" href="?request_delete_chat={chat_id}" target="_self"
               title="Delete chat" aria-label="Delete chat">
                X
            </a>
        </div>
    """


def render_chat_button(label: str, chat_id: int) -> None:
    active = chat_id == st.session_state.active_chat_id
    st.sidebar.markdown(chat_row_html(label, chat_id, active), unsafe_allow_html=True)


def project_chat_link_html(label: str, chat_id: int, active: bool) -> str:
    active_class = " active" if active else ""
    safe_label = escape(label)
    return (
        f'<a class="project-chat-link{active_class}" '
        f'href="?open_chat={chat_id}" target="_self">{safe_label}</a>'
    )


def project_folder_html(project, chats: list, selected_project_id: int | None) -> str:
    expanded = " open" if project.id == selected_project_id else ""
    safe_name = escape(project.name)
    chat_html = "\n".join(
        project_chat_link_html(chat.title, chat.id, chat.id == st.session_state.active_chat_id)
        for chat in chats
    )
    return f"""
        <details class="project-folder"{expanded}>
            <summary>
                <svg class="project-folder-icon" viewBox="0 0 24 24" aria-hidden="true">
                    <path
                        d="M3 8a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="1.9"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                    />
                    <path
                        d="M3 8h18v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"
                        fill="none"
                        stroke="currentColor"
                        stroke-width="1.9"
                        stroke-linecap="round"
                        stroke-linejoin="round"
                    />
                </svg>
                <span class="project-folder-title">{safe_name}</span>
                <a class="project-folder-delete"
                   href="?request_delete_project={project.id}"
                   target="_self"
                   title="Delete project"
                   aria-label="Delete project">X</a>
            </summary>
            <a class="project-folder-new-chat"
               href="?new_project_chat={project.id}"
               target="_self">+ New Chat</a>
            {chat_html}
        </details>
    """


def render_sidebar() -> None:
    st.sidebar.markdown('<div class="sidebar-title">Neo</div>', unsafe_allow_html=True)
    if st.sidebar.button("+ New Chat", use_container_width=True):
        create_new_chat(st.session_state.selected_project_id)
        st.rerun()

    with session_scope() as db:
        store = MemoryStore(db)
        st.sidebar.markdown('<div class="sidebar-section">Projects</div>', unsafe_allow_html=True)
        with st.sidebar.expander("+ New Project", expanded=False):
            with st.form("new-project-form", clear_on_submit=True):
                name = st.text_input("Project name", placeholder="Research, work, ideas...")
                submitted = st.form_submit_button("Create")
                if submitted:
                    create_project(name)
                    st.rerun()

        projects = store.list_projects(ProjectStatus.ACTIVE)
        if not projects:
            st.sidebar.caption("No projects yet.")
        for project in projects:
            active_project = project.id == st.session_state.selected_project_id
            project_class = "sidebar-project active" if active_project else "sidebar-project"
            st.sidebar.markdown(
                f"""
                <a class="{project_class}" href="?select_project={project.id}" target="_self">
                    <span class="sidebar-folder">▣</span>
                    <span>{escape(project.name)}</span>
                </a>
                """,
                unsafe_allow_html=True,
            )
            for chat in store.list_chats(
                project_id=project.id,
                with_messages_only=True,
                limit=12,
            ):
                render_chat_button(chat.title, chat.id)

        st.sidebar.markdown('<div class="sidebar-section">Chats</div>', unsafe_allow_html=True)
        chats = store.list_chats(unprojected_only=True, with_messages_only=True, limit=20)
        if not chats:
            st.sidebar.caption("No chats yet.")
        for chat in chats:
            render_chat_button(chat.title, chat.id)

    st.sidebar.markdown('<div class="sidebar-section">Skills</div>', unsafe_allow_html=True)
    skill = st.sidebar.selectbox(
        "Open skill",
        ["Choose a skill", "Memory"],
        label_visibility="collapsed",
    )
    if skill == "Memory":
        st.session_state.show_memory = True


def render_sidebar_v2() -> None:
    st.sidebar.markdown('<div class="sidebar-title">Neo</div>', unsafe_allow_html=True)
    if st.sidebar.button("+ New Chat", use_container_width=True):
        create_new_chat(st.session_state.selected_project_id)
        st.rerun()
    if st.sidebar.button("+ New Project", use_container_width=True):
        st.session_state.show_new_project_form = not st.session_state.show_new_project_form
        st.rerun()

    if st.session_state.show_new_project_form:
        with st.sidebar.form("new-project-form-v2", clear_on_submit=True):
            name = st.text_input("Project name", placeholder="Research, work, ideas...")
            submitted = st.form_submit_button("Create")
            if submitted:
                create_project(name)
                st.session_state.show_new_project_form = False
                st.rerun()

    with session_scope() as db:
        store = MemoryStore(db)
        st.sidebar.markdown('<div class="sidebar-section">Projects</div>', unsafe_allow_html=True)
        projects = store.list_projects(ProjectStatus.ACTIVE)
        if not projects:
            st.sidebar.caption("No projects yet.")
        for project in projects:
            chats = store.list_chats(project_id=project.id, with_messages_only=True, limit=12)
            st.sidebar.markdown(
                project_folder_html(project, chats, st.session_state.selected_project_id),
                unsafe_allow_html=True,
            )

        st.sidebar.markdown('<div class="sidebar-section">Chats</div>', unsafe_allow_html=True)
        chats = store.list_chats(unprojected_only=True, with_messages_only=True, limit=20)
        if not chats:
            st.sidebar.caption("No chats yet.")
        for chat in chats:
            render_chat_button(chat.title, chat.id)

    st.sidebar.markdown('<div class="sidebar-section">Skills</div>', unsafe_allow_html=True)
    skill = st.sidebar.selectbox(
        "Open skill",
        ["Choose a skill", "Memory"],
        label_visibility="collapsed",
        key="skills-v2",
    )
    if skill == "Memory":
        st.session_state.show_memory = True


@st.dialog("Confirm deletion")
def confirm_delete_dialog() -> None:
    chat_id = st.session_state.pending_delete_chat_id
    project_id = st.session_state.pending_delete_project_id
    if chat_id is None and project_id is None:
        return

    if chat_id is not None:
        with session_scope() as db:
            chat = MemoryStore(db).get_chat(chat_id)
            label = chat.title if chat else "this chat"
        st.markdown(f"Delete chat **{label}**?")
        st.caption("This will permanently delete the chat and its messages.")
        confirm, cancel = st.columns(2)
        if confirm.button("Confirm", type="primary"):
            delete_chat(chat_id)
            st.session_state.pending_delete_chat_id = None
            st.rerun()
        if cancel.button("Cancel"):
            st.session_state.pending_delete_chat_id = None
            st.rerun()
        return

    with session_scope() as db:
        store = MemoryStore(db)
        if hasattr(store, "get_project"):
            project = store.get_project(project_id)
        else:
            project = db.get(Project, project_id)
        label = project.name if project else "this project"
        chat_count = len(store.list_chats(project_id=project_id, limit=500))
    st.markdown(f"Delete project **{label}**?")
    st.caption(f"This will permanently delete the project and {chat_count} chat(s) inside it.")
    confirm, cancel = st.columns(2)
    if confirm.button("Confirm", type="primary"):
        delete_project(project_id)
        st.session_state.pending_delete_project_id = None
        st.rerun()
    if cancel.button("Cancel"):
        st.session_state.pending_delete_project_id = None
        st.rerun()


def render_memory_records(records: list, empty: str, fields: list[str]) -> None:
    if not records:
        st.caption(empty)
        return
    for record in records:
        chunks = []
        for field in fields:
            value = getattr(record, field, None)
            if value is not None:
                chunks.append(f"**{field.replace('_', ' ').title()}**: {value}")
        st.markdown(f"<div class='neo-card'>{'<br>'.join(chunks)}</div>", unsafe_allow_html=True)


@st.dialog("Memory")
def memory_dialog() -> None:
    with session_scope() as db:
        store = MemoryStore(db)
        reviewer = MemoryReviewService()
        tabs = st.tabs(
            ["Profile", "Preferences", "Goals", "Projects", "Events", "Memories", "Review"]
        )

        with tabs[0]:
            render_memory_records(store.list_profile(), "No profile facts yet.", ["key", "value"])
        with tabs[1]:
            render_memory_records(
                store.list_preferences(),
                "No preferences yet.",
                ["category", "value", "importance"],
            )
        with tabs[2]:
            render_memory_records(
                store.list_goals(GoalStatus.ACTIVE),
                "No active goals yet.",
                ["goal", "description", "priority"],
            )
        with tabs[3]:
            render_memory_records(
                store.list_projects(ProjectStatus.ACTIVE),
                "No active projects yet.",
                ["name", "description", "priority"],
            )
        with tabs[4]:
            render_memory_records(
                store.list_events(),
                "No events yet.",
                ["event", "description", "event_date", "importance"],
            )
        with tabs[5]:
            render_memory_records(
                store.list_memories(limit=100),
                "No memories yet.",
                ["memory_text", "memory_type", "importance"],
            )
        with tabs[6]:
            candidates = store.list_candidates(CandidateStatus.PENDING, limit=50)
            if not candidates:
                st.caption("No pending memory to review.")
            for candidate in candidates:
                data = MemoryCandidateRead.model_validate(candidate)
                candidate_type = data.candidate_type.value
                st.markdown(
                    f"<div class='neo-card'><span class='neo-pill'>{candidate_type}</span>"
                    f"{data.candidate_text}</div>",
                    unsafe_allow_html=True,
                )
                accept, reject = st.columns(2)
                if accept.button("Accept", key=f"memory-accept-{data.id}"):
                    reviewer.review(
                        store,
                        MemoryReviewRequest(
                            candidate_id=data.id,
                            decision=CandidateStatus.ACCEPTED,
                        ),
                    )
                    db.commit()
                    st.rerun()
                if reject.button("Reject", key=f"memory-reject-{data.id}"):
                    reviewer.review(
                        store,
                        MemoryReviewRequest(
                            candidate_id=data.id,
                            decision=CandidateStatus.REJECTED,
                        ),
                    )
                    db.commit()
                    st.rerun()

    if st.button("Close"):
        st.session_state.show_memory = False
        st.rerun()


def load_active_messages() -> list[dict[str, str]]:
    with session_scope() as db:
        messages = MemoryStore(db).list_chat_messages(st.session_state.active_chat_id)
        return [{"role": message.role, "content": message.content} for message in messages]


def run_chat(prompt: str) -> None:
    with session_scope() as db:
        service = NeoChatService(db, ollama=OllamaClient(model=MODEL_NAME, base_url=OLLAMA_URL))
        service.send_message(st.session_state.active_chat_id, prompt)


def main() -> None:
    st.set_page_config(
        page_title="Neo",
        page_icon="N",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_theme()
    initialize_state()
    handle_sidebar_query_actions()
    ensure_active_chat()
    render_sidebar_v2()

    if (
        st.session_state.pending_delete_chat_id is not None
        or st.session_state.pending_delete_project_id is not None
    ):
        confirm_delete_dialog()

    if st.session_state.show_memory:
        memory_dialog()

    st.markdown('<div class="neo-shell">', unsafe_allow_html=True)
    st.markdown('<h1 class="neo-title">Neo</h1>', unsafe_allow_html=True)
    st.markdown(
        '<p class="neo-subtitle">Your local personal AI assistant</p>',
        unsafe_allow_html=True,
    )

    messages = load_active_messages()
    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])

    if not messages:
        st.markdown(
            """
            <div class="neo-status">
              <span class="neo-pill">READY</span>
              Start a conversation or open a previous chat from the sidebar.
            </div>
            """,
            unsafe_allow_html=True,
        )

    prompt = st.chat_input("Message Neo")
    if prompt:
        st.chat_message("user").markdown(prompt)
        try:
            with st.spinner("Neo is thinking..."):
                run_chat(prompt)
            st.rerun()
        except requests.RequestException as exc:
            st.error(
                "Ollama is not reachable or the model is unavailable. "
                f"Expected {MODEL_NAME} at {OLLAMA_URL}. Details: {exc}"
            )

    st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
