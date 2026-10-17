/**
 * Dates the way Apple Notes shows them.
 *
 * The list groups notes into Pinned, Today, Yesterday, Previous 7 Days, Previous
 * 30 Days, then each earlier month of this year by name, then earlier years. A
 * row shows the time for today, "Yesterday", the weekday within a week, and a
 * short date before that. Everything is in the viewer's local time and locale.
 */

const DAY = 24 * 60 * 60 * 1000;

function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function parse(iso) {
  if (!iso) return null;
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** Which section a date belongs to: { key, label }. */
export function dateGroup(iso, now = new Date()) {
  const date = parse(iso) ?? now;
  const today = startOfDay(now);
  if (date >= today) return { key: "today", label: "Today" };
  const days = Math.round((today - startOfDay(date)) / DAY);
  if (days <= 1) return { key: "yesterday", label: "Yesterday" };
  if (days <= 7) return { key: "week", label: "Previous 7 Days" };
  if (days <= 30) return { key: "month", label: "Previous 30 Days" };
  if (date.getFullYear() === now.getFullYear()) {
    return {
      key: `m-${date.getMonth()}`,
      label: date.toLocaleDateString(undefined, { month: "long" }),
    };
  }
  return { key: `y-${date.getFullYear()}`, label: String(date.getFullYear()) };
}

/**
 * Notes in display sections. Pinned notes come first (except where pinning does
 * not apply); the rest are grouped by date unless grouping is off or the list is
 * sorted by title.
 */
export function groupNotes(items, { now = new Date(), sortBy = "edited", groupByDate = true, pinning = true } = {}) {
  const field = sortBy === "created" ? "created_at" : "updated_at";
  const pinned = pinning ? items.filter((item) => item.pinned) : [];
  const rest = pinning ? items.filter((item) => !item.pinned) : items;
  const sections = [];
  if (pinned.length) sections.push({ key: "pinned", label: "Pinned", items: pinned });
  if (!groupByDate || sortBy === "title") {
    if (rest.length) sections.push({ key: "notes", label: pinned.length ? "Notes" : null, items: rest });
    return sections;
  }
  let current = null;
  for (const item of rest) {
    const group = dateGroup(item[field], now);
    if (!current || current.key !== group.key) {
      current = { ...group, items: [] };
      sections.push(current);
    }
    current.items.push(item);
  }
  return sections;
}

/** The short date on a list row. */
export function rowDate(iso, now = new Date()) {
  const date = parse(iso);
  if (!date) return "";
  const today = startOfDay(now);
  if (date >= today) return date.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  const days = Math.round((today - startOfDay(date)) / DAY);
  if (days <= 1) return "Yesterday";
  if (days < 7) return date.toLocaleDateString(undefined, { weekday: "long" });
  return date.toLocaleDateString(undefined, { day: "numeric", month: "numeric", year: "2-digit" });
}

/** The full date at the top of a note: "15 October 2026 at 14:05". */
export function longDate(iso) {
  const date = parse(iso);
  if (!date) return "";
  return new Intl.DateTimeFormat(undefined, { dateStyle: "long", timeStyle: "short" }).format(date);
}
