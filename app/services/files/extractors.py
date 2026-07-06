from __future__ import annotations

from app.services.files.safety import is_preview_supported


def extract_text(filename: str, content: bytes, max_chars: int) -> tuple[str | None, dict]:
    if not is_preview_supported(filename):
        return None, {"preview_supported": False, "truncated": False}
    if b"\x00" in content[:8192]:
        return None, {"preview_supported": False, "binary_detected": True, "truncated": False}
    encoding = "utf-8"
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        encoding = "latin-1"
        text = content.decode("latin-1")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    original_chars = len(text)
    truncated = original_chars > max_chars
    if truncated:
        text = text[:max_chars]
    return text, {
        "preview_supported": True,
        "encoding": encoding,
        "truncated": truncated,
        "extracted_chars": len(text),
        "original_chars_estimate": original_chars,
    }
