from app.services.context_memory.redaction import redact


def safe(value):
    return redact(value)
