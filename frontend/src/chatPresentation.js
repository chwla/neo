export function formatTokens(message) {
  return Number.isFinite(message.total_tokens) ? `${message.total_tokens} tokens` : null;
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
