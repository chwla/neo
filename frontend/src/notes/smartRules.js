/**
 * Smart folder rules on this side: what each rule can say (for the editor),
 * and whether a note in the list matches (so a note pinned, moved or tagged
 * while a smart folder is open joins or leaves it at once). The server is the
 * authority (app/services/notes/smart.py); these two must agree, and
 * tests/notes-smart-rules.test.mjs holds them to the same cases.
 *
 * `tzOffset` is local time minus UTC in minutes (UTC+2 is 120), the opposite
 * sign of Date#getTimezoneOffset.
 */

export const localTzOffset = () => -new Date().getTimezoneOffset();

/** The fields, their operations, and what value each operation takes. */
export const RULE_FIELDS = [
  {
    field: "tags",
    label: "Tags",
    ops: [
      { op: "includes_any", label: "include any of", value: "tags" },
      { op: "includes_all", label: "include all of", value: "tags" },
      { op: "excludes", label: "don't include", value: "tags" },
      { op: "has_any", label: "any tag" },
      { op: "has_none", label: "no tags" },
    ],
  },
  ...["created", "edited"].map((field) => ({
    field,
    label: field === "created" ? "Date Created" : "Date Edited",
    ops: [
      { op: "today", label: "today" },
      { op: "yesterday", label: "yesterday" },
      { op: "last_days", label: "in the last", value: "days" },
      { op: "before", label: "before", value: "date" },
      { op: "after", label: "after", value: "date" },
    ],
  })),
  {
    field: "checklists",
    label: "Checklists",
    ops: [
      { op: "has", label: "has a checklist" },
      { op: "has_unchecked", label: "has unchecked items" },
      { op: "all_checked", label: "all items checked" },
      { op: "none", label: "no checklist" },
    ],
  },
  {
    field: "attachments",
    label: "Attachments",
    ops: [
      { op: "any", label: "any attachment" },
      { op: "images", label: "pictures" },
      { op: "files", label: "files" },
      { op: "sketches", label: "sketches" },
      { op: "none", label: "no attachments" },
    ],
  },
  {
    field: "folder",
    label: "Folder",
    ops: [
      { op: "in", label: "is in", value: "folders" },
      { op: "not_in", label: "is not in", value: "folders" },
    ],
  },
  { field: "pinned", label: "Pinned", ops: [{ op: "is", label: "is", value: "bool" }] },
  { field: "locked", label: "Locked", ops: [{ op: "is", label: "is", value: "bool" }] },
];

export function fieldSpec(field) {
  return RULE_FIELDS.find((spec) => spec.field === field) ?? RULE_FIELDS[0];
}

export function opSpec(field, op) {
  const spec = fieldSpec(field);
  return spec.ops.find((entry) => entry.op === op) ?? spec.ops[0];
}

/** A rule with a sensible value for its operation, as the editor starts one. */
export function defaultRule(field = "tags", op = null) {
  const spec = opSpec(field, op ?? fieldSpec(field).ops[0].op);
  const rule = { field, op: spec.op };
  if (spec.value === "tags" || spec.value === "folders") rule.value = [];
  if (spec.value === "days") rule.value = 7;
  if (spec.value === "date") rule.value = new Date().toISOString().slice(0, 10);
  if (spec.value === "bool") rule.value = true;
  return rule;
}

/** Whether a rule is complete enough to save. */
export function ruleIsComplete(rule) {
  const spec = opSpec(rule.field, rule.op);
  if (spec.value === "tags" || spec.value === "folders") return Array.isArray(rule.value) && rule.value.length > 0;
  if (spec.value === "days") return Number.isInteger(rule.value) && rule.value >= 1 && rule.value <= 3650;
  if (spec.value === "date") return /^\d{4}-\d{2}-\d{2}$/.test(String(rule.value));
  if (spec.value === "bool") return typeof rule.value === "boolean";
  return true;
}

function dayStart(now, tzOffset, daysBack = 0) {
  // The local day containing `now`, shifted back, as a UTC instant.
  const local = new Date(now.getTime() + tzOffset * 60_000);
  const start = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate() - daysBack);
  return start - tzOffset * 60_000;
}

function localDay(day, tzOffset) {
  const [y, m, d] = String(day).split("-").map(Number);
  return Date.UTC(y, m - 1, d) - tzOffset * 60_000;
}

function matchesRule(rule, note, now, tzOffset) {
  const tags = new Set(note.tags ?? []);
  switch (rule.field) {
    case "tags": {
      const wanted = (rule.value ?? []).map((tag) => String(tag).toLowerCase().replace(/^#+/, ""));
      if (rule.op === "has_any") return tags.size > 0;
      if (rule.op === "has_none") return tags.size === 0;
      if (rule.op === "includes_any") return wanted.some((tag) => tags.has(tag));
      if (rule.op === "includes_all") return wanted.every((tag) => tags.has(tag));
      return !wanted.some((tag) => tags.has(tag));
    }
    case "created":
    case "edited": {
      const at = Date.parse(rule.field === "created" ? note.created_at : note.updated_at);
      if (Number.isNaN(at)) return false;
      if (rule.op === "today") return at >= dayStart(now, tzOffset);
      if (rule.op === "yesterday") return at >= dayStart(now, tzOffset, 1) && at < dayStart(now, tzOffset);
      if (rule.op === "last_days") return at >= now.getTime() - rule.value * 86_400_000;
      if (rule.op === "before") return at < localDay(rule.value, tzOffset);
      return at >= localDay(rule.value, tzOffset) + 86_400_000;
    }
    case "checklists": {
      const total = note.checklist_total ?? 0;
      const done = note.checklist_done ?? 0;
      if (rule.op === "has") return total > 0;
      if (rule.op === "has_unchecked") return total > done;
      if (rule.op === "all_checked") return total > 0 && done === total;
      return total === 0;
    }
    case "attachments": {
      const images = Boolean(note.has_images);
      const files = Boolean(note.has_files);
      const sketches = Boolean(note.has_sketches);
      if (rule.op === "any") return images || files || sketches;
      if (rule.op === "images") return images;
      if (rule.op === "files") return files;
      if (rule.op === "sketches") return sketches;
      return !images && !files && !sketches;
    }
    case "folder": {
      const inFolders = (rule.value ?? []).some((id) => (id === "default" ? !note.folder_id : note.folder_id === id));
      return rule.op === "in" ? inFolders : !inFolders;
    }
    case "pinned":
      return Boolean(note.pinned) === Boolean(rule.value);
    case "locked":
      return (note.content_format === "encrypted") === Boolean(rule.value);
    default:
      return false;
  }
}

/** Whether a note in the list belongs in a smart folder with these rules. */
export function matchesRules(rules, note, { now = new Date(), tzOffset = localTzOffset() } = {}) {
  if (!rules?.rules?.length || note.deleted || note.archived) return false;
  const results = rules.rules.map((rule) => matchesRule(rule, note, now, tzOffset));
  return rules.match === "any" ? results.some(Boolean) : results.every(Boolean);
}
