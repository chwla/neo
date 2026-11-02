/**
 * Copy text, and say whether it worked.
 *
 * `navigator.clipboard` exists only in a secure context (https, or http on
 * localhost), and a browser may still refuse it. The fallback is the
 * selection-based copy every supported browser keeps. When both fail the caller
 * is told, so it can say so instead of claiming a copy that never happened.
 */
export const COPY_FAILED_MESSAGE = "Couldn't copy. Select the text and copy it yourself.";

export async function copyText(text) {
  const value = String(text ?? "");
  try {
    const clipboard = globalThis.navigator?.clipboard;
    if (typeof clipboard?.writeText === "function") {
      await clipboard.writeText(value);
      return true;
    }
  } catch {
    // Refused or unavailable; try the fallback below.
  }
  return selectionCopy(value);
}

function selectionCopy(value) {
  const doc = globalThis.document;
  if (!doc?.body || typeof doc.execCommand !== "function") return false;
  const area = doc.createElement("textarea");
  area.value = value;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  doc.body.appendChild(area);
  area.select();
  try {
    return doc.execCommand("copy") === true;
  } catch {
    return false;
  } finally {
    area.remove();
  }
}
