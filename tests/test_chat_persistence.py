from app.models.enums import ProjectStatus
from app.repositories.memory_store import MemoryStore
from app.services.chat import NeoChatService


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


class FakeOllama:
    def chat(self, _messages):
        return "Saved answer"


class TransactionCheckingOllama:
    def __init__(self, db_session) -> None:
        self.db_session = db_session
        self.in_transaction_during_chat = None

    def chat(self, _messages):
        self.in_transaction_during_chat = self.db_session.in_transaction()
        return "No lock held"


class MemoryAwareOllama:
    def __init__(self) -> None:
        self.chat_system_prompts: list[str] = []

    def chat(self, messages, temperature=0.4):
        system_prompt = messages[0].content
        if "You are Neo" in system_prompt:
            self.chat_system_prompts.append(system_prompt)
            has_name = '"key": "name"' in system_prompt and '"value": "Soham"' in system_prompt
            has_age = '"key": "age"' in system_prompt and '"value": "21"' in system_prompt
            if has_name and has_age:
                return "Your name is Soham and you are 21."
        return "Stored."


class FailingExtractor:
    def extract_with_llm(self, _request, _ollama):
        return object()

    def persist_and_accept(self, _store, _extraction):
        raise RuntimeError("memory extraction failed")


def test_chat_reply_survives_memory_extraction_failure(db_session) -> None:
    store = MemoryStore(db_session)
    chat = store.create_chat()
    db_session.commit()
    service = NeoChatService(db_session, ollama=FakeOllama(), extractor=FailingExtractor())

    reply = service.send_message(chat.id, "Remember that I like concise answers.")

    messages = store.list_chat_messages(chat.id)
    assert reply == "Saved answer"
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[-1].content == "Saved answer"


def test_chat_does_not_hold_write_transaction_during_ollama_call(db_session) -> None:
    store = MemoryStore(db_session)
    chat = store.create_chat()
    db_session.commit()
    ollama = TransactionCheckingOllama(db_session)
    service = NeoChatService(db_session, ollama=ollama, extractor=FailingExtractor())

    service.send_message(chat.id, "This should not lock sqlite while Ollama runs.")

    assert ollama.in_transaction_during_chat is False


def test_name_and_age_memory_carries_into_new_chat(db_session) -> None:
    store = MemoryStore(db_session)
    first_chat = store.create_chat()
    db_session.commit()
    ollama = MemoryAwareOllama()
    service = NeoChatService(db_session, ollama=ollama)

    service.send_message(first_chat.id, "My name is Soham and I am 21 years old.")

    profile = {fact.key: fact.value for fact in store.list_profile()}
    memory_texts = [memory.memory_text for memory in store.list_memories()]
    assert profile == {"age": "21", "name": "Soham"}
    assert "name = Soham" in memory_texts
    assert "age = 21" in memory_texts

    second_chat = store.create_chat()
    db_session.commit()
    reply = service.send_message(second_chat.id, "What is my name and age?")

    assert reply == "Your name is Soham and you are 21."
    assert '"key": "name"' in ollama.chat_system_prompts[-1]
    assert '"value": "Soham"' in ollama.chat_system_prompts[-1]
    assert '"key": "age"' in ollama.chat_system_prompts[-1]
    assert '"value": "21"' in ollama.chat_system_prompts[-1]
