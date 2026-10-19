/**
 * What the chat effort control says about Low and High.
 *
 * Some models cannot switch reasoning off, so Low cannot make them skip it. The
 * backend marks a reply's timings with `model_always_reasons` only after it saw
 * a model reason when asked not to. This reads the latest Low reply from the
 * chosen model and never guesses from the model's name.
 */

const LOW =
  "Quick: shorter answers, usually faster. Neo searches for current information when web search is on.";
const HIGH =
  "Detailed: fuller answers and more sources when web search is on. This takes longer.";

export const ALWAYS_REASONS_NOTE = "This model takes time to think even in Quick mode.";

/**
 * Whether the latest Low reply from `modelName` reasoned although Low asked it
 * not to. Replies from another model are skipped; with no model name, the
 * latest Low reply decides.
 */
export function modelAlwaysReasons(messages, modelName = "") {
  const list = Array.isArray(messages) ? messages : [];
  for (let index = list.length - 1; index >= 0; index -= 1) {
    const message = list[index];
    const timings = message?.metadata?.timings;
    if (message?.role !== "assistant" || timings?.effort !== "low") {
      continue;
    }
    if (modelName && message.model_name && message.model_name !== modelName) {
      continue;
    }
    return Boolean(timings.model_always_reasons);
  }
  return false;
}

export function effortTooltip(alwaysReasons = false) {
  return [LOW, alwaysReasons ? ALWAYS_REASONS_NOTE : null, HIGH].filter(Boolean).join(" ");
}
