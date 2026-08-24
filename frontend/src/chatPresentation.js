import { renderMarkdown } from "./markdown.js";

/**
 * Chat bodies are Markdown, whoever wrote them.
 *
 * A user pasting a fenced block expects the same code block the model gets, so both
 * roles go through one renderer rather than one side rendering and the other showing
 * raw syntax. The renderer escapes before it emits any tag, so model output is no
 * more trusted here than it was as plain text.
 */
export function renderMessageHtml(content) {
  return renderMarkdown(content);
}

export function formatTokens(message) {
  return Number.isFinite(message.total_tokens) ? `${message.total_tokens} tokens` : null;
}

/** What this one reply cost -- completion tokens only, not the whole prompt it read. */
export function formatOutputTokens(message) {
  return Number.isFinite(message.completion_tokens)
    ? `${formatCompactTokens(message.completion_tokens)} tokens`
    : null;
}

/** "344" under 1000, "12.4k" / "128k" above -- the same shorthand the popover and its trigger both use. */
export function formatCompactTokens(value) {
  if (!Number.isFinite(value)) {
    return null;
  }
  if (value < 1000) {
    return String(value);
  }
  const thousands = value / 1000;
  return `${Number.isInteger(thousands) ? thousands : thousands.toFixed(1)}k`;
}

/**
 * A model's context window, if this message's provider+model was ever seen by the
 * LLM registry (Settings -> LLM Providers). `provider_name`/`model_name` on a chat
 * message are the literal provider-type and model-id strings the registry keys on
 * (see `_sync_model_to_picker` server-side), so the pair is a reliable join key --
 * but a legacy-only model with no registry entry genuinely has no known window.
 */
export function resolveContextWindow(message, contextWindowIndex) {
  if (!contextWindowIndex) {
    return null;
  }
  return contextWindowIndex.get(`${message.provider_name}::${message.model_name}`) ?? null;
}

/** Every turn's prompt+completion tokens, summed across the whole loaded session. */
export function sumTotalTokens(messages) {
  return messages.reduce(
    (sum, message) => (Number.isFinite(message.total_tokens) ? sum + message.total_tokens : sum),
    0,
  );
}

export function formatDuration(durationMs) {
  if (!Number.isFinite(durationMs)) {
    return null;
  }
  if (durationMs < 1000) {
    return `${durationMs} ms`;
  }
  const seconds = durationMs / 1000;
  return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)} s`;
}

/**
 * Parse a timestamp Neo's API produced.
 *
 * Those are UTC but serialize without a marker, and an unmarked timestamp is
 * *local* time to `Date.parse`. Reading one as local shifts every stamp by the
 * viewer's offset, so an unmarked value is pinned to UTC before parsing.
 */
export function parseNeoTimestamp(value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return Number.NaN;
  }
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? raw : `${raw}Z`;
  return Date.parse(normalized);
}

/** Wall-clock time on the bubble, in the viewer's zone, the way a messaging app stamps one. */
export function formatMessageTime(value) {
  const parsed = parseNeoTimestamp(value);
  if (Number.isNaN(parsed)) {
    return null;
  }
  return new Date(parsed).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export function formatResponseKind(message) {
  const labels = {
    connector: "Connector",
    direct_memory: "Memory",
    internal_action: "Neo action",
    local_datetime: "Local date & time",
    structured_currency: "Currency",
    structured_weather: "Weather",
    web_search: "Web search",
  };
  if (message.response_kind && labels[message.response_kind]) {
    return labels[message.response_kind];
  }
  if (message.model_name) {
    return message.provider_name
      ? `${message.provider_name} / ${message.model_name}`
      : message.model_name;
  }
  return message.response_kind ? message.response_kind.replaceAll("_", " ") : null;
}

export function splitGeneratedText(rawContent) {
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
