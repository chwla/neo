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
  const source = String(content ?? "");
  // Very long bodies are rendered but not kept: they would crowd out hundreds
  // of ordinary messages for one rarely re-shown screen.
  if (source.length > HTML_CACHE_MAX_SOURCE) return renderMarkdown(source);
  const hit = htmlCache.get(source);
  if (hit !== undefined) {
    // Re-inserted, so the Map's insertion order is least-recently-used first.
    htmlCache.delete(source);
    htmlCache.set(source, hit);
    return hit;
  }
  const html = renderMarkdown(source);
  htmlCache.set(source, html);
  htmlCacheChars += source.length + html.length;
  while (htmlCache.size > HTML_CACHE_MAX_ENTRIES || htmlCacheChars > HTML_CACHE_MAX_CHARS) {
    const [oldest, oldestHtml] = htmlCache.entries().next().value;
    htmlCache.delete(oldest);
    htmlCacheChars -= oldest.length + oldestHtml.length;
  }
  return html;
}

/**
 * Saved messages are rendered once per distinct text.
 *
 * A chat switch, a turn ending or a compaction replaces every message object,
 * and each row used to re-run the Markdown renderer on mount or re-render. The
 * renderer is a pure function of the text alone -- no options, no locale, no
 * clock -- so the text is the complete key: any option it ever gains must join
 * the key here. Bounded by count and by characters held (source plus HTML), so
 * a long session cannot grow it without limit.
 */
const HTML_CACHE_MAX_ENTRIES = 500;
const HTML_CACHE_MAX_CHARS = 4_000_000;
const HTML_CACHE_MAX_SOURCE = 100_000;
const htmlCache = new Map();
let htmlCacheChars = 0;

/**
 * Text that is still arriving: rendered fresh, never cached. Every streamed
 * frame is a new prefix, and keeping them would evict saved messages to hold
 * strings nobody will ask for again.
 */
export function renderStreamingHtml(content) {
  return renderMarkdown(content);
}

/** Drop every cached rendering, e.g. when the profile changes. */
export function clearMessageHtmlCache() {
  htmlCache.clear();
  htmlCacheChars = 0;
}

/** How many renderings are held, and how many characters they cost. For tests. */
export function messageHtmlCacheStats() {
  return { entries: htmlCache.size, chars: htmlCacheChars };
}

/**
 * What is new to the person looking, as presentation keys.
 *
 * Only the code that creates content marks it -- the bubble just sent, the reply
 * that send starts, an agent run it starts -- so a saved row arriving from a
 * reload is never mistaken for news. A row reads its key once, when it mounts,
 * and the key is removed once that mount commits: a later remount (a chat
 * switch, a workspace round trip, a reload) finds nothing and stays still, and
 * the saved row that replaces a pending one has a key nobody marked.
 */
export function createArrivals(limit = 64) {
  const keys = new Set();
  return {
    mark(key) {
      keys.delete(key);
      keys.add(key);
      // Bounded: a mark nobody consumes (a send that failed before its reply
      // existed) must not accumulate for the life of the tab.
      while (keys.size > limit) keys.delete(keys.values().next().value);
    },
    has: (key) => keys.has(key),
    consume: (key) => {
      keys.delete(key);
    },
    get size() {
      return keys.size;
    },
  };
}

/** The key an agent run's row is marked under, known as soon as the run starts. */
export function agentArrivalKey(sessionId) {
  return `agent:${sessionId}`;
}

/** The key a transcript row is marked under: an agent row by its run, any other by its id. */
export function arrivalKeyFor(message) {
  const sessionId = message?.agent?.session?.id;
  return sessionId != null ? agentArrivalKey(sessionId) : `message:${message?.id}`;
}

/** The key for the pending reply of the turn started at `startedAt` in `chatId`. */
export function replyArrivalKey(chatId, startedAt) {
  return `turn:${chatId}:${startedAt}`;
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
  // One formatter for every bubble: `toLocaleTimeString` with these options
  // builds an equivalent one on each call. Created on first use, so the zone
  // and locale are the viewer's at that moment, as before.
  timeFormat ??= new Intl.DateTimeFormat([], { hour: "2-digit", minute: "2-digit" });
  return timeFormat.format(new Date(parsed));
}
let timeFormat = null;

export function formatResponseKind(message) {
  const labels = {
    connector: "Connector",
    direct_memory: "Memory",
    internal_action: "Neo action",
    local_datetime: "Local date & time",
    structured_currency: "Currency",
    structured_weather: "Weather",
    web_search: "Web search",
    calendar_proposal: "Calendar proposal",
    calendar_proposal_reask: "Calendar",
    calendar_clarification: "Calendar",
    calendar_read: "Calendar",
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
