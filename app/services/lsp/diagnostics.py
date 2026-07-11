from app.services.context_memory.redaction import redact_text
from app.services.lsp import store


def publish(workspace_id, file_path, language, diagnostics):
    store.replace_diagnostics(workspace_id, file_path, language, diagnostics, redact_text)
