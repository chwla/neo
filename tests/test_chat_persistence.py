from app.models.enums import ProjectStatus
from app.repositories.memory_store import MemoryStore


def test_create_chat_and_persist_messages(db_session) -> None:
    store = MemoryStore(db_session)
    chat = store.create_chat()
    store.add_chat_message(chat.id, "user", "Hello Neo")
    store.add_chat_message(chat.id, "assistant", "Hello")

    messages = store.list_chat_messages(chat.id)

    assert chat.title == "New chat"
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].content == "Hello Neo"


def test_rename_chat_from_first_prompt(db_session) -> None:
    store = MemoryStore(db_session)
    chat = store.create_chat()

    store.rename_chat_from_prompt(chat.id, "Help me design Neo memory")
    store.rename_chat_from_prompt(chat.id, "This should not replace the title")

    assert chat.title == "Help me design Neo memory"


def test_assign_chat_to_project_and_list_nested_chats(db_session) -> None:
    store = MemoryStore(db_session)
    project = store.create_project("Neo")
    chat = store.create_chat()
    store.add_chat_message(chat.id, "user", "Project chat")

    store.assign_chat_to_project(chat.id, project.id)

    assert store.list_projects(ProjectStatus.ACTIVE)[0].name == "Neo"
    assert store.list_chats(project_id=project.id, with_messages_only=True)[0].id == chat.id
    assert store.list_chats(unprojected_only=True, with_messages_only=True) == []


def test_empty_chats_are_hidden_when_requested(db_session) -> None:
    store = MemoryStore(db_session)
    empty_chat = store.create_chat()
    non_empty_chat = store.create_chat()
    store.add_chat_message(non_empty_chat.id, "user", "I should appear")

    visible = store.list_chats(with_messages_only=True)

    assert empty_chat not in visible
    assert non_empty_chat in visible


def test_delete_chat_removes_chat_and_messages(db_session) -> None:
    store = MemoryStore(db_session)
    chat = store.create_chat()
    store.add_chat_message(chat.id, "user", "delete this")

    store.delete_chat(chat.id)

    assert store.get_chat(chat.id) is None
    assert store.list_chat_messages(chat.id) == []


def test_delete_project_removes_project_and_nested_chats(db_session) -> None:
    store = MemoryStore(db_session)
    project = store.create_project("Delete me")
    chat = store.create_chat(project_id=project.id)
    store.add_chat_message(chat.id, "user", "inside project")

    store.delete_project(project.id)

    assert store.get_project(project.id) is None
    assert store.get_chat(chat.id) is None
    assert store.list_chat_messages(chat.id) == []


def test_memory_modal_sources_are_retrievable(db_session) -> None:
    store = MemoryStore(db_session)
    store.create_project("Research")
    store.create_chat()

    assert store.list_projects(ProjectStatus.ACTIVE)
    assert store.list_chats()
    assert store.list_profile() == []
    assert store.list_preferences() == []
    assert store.list_goals() == []
    assert store.list_events() == []
    assert store.list_memories() == []
