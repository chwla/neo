/**
 * Turning the catalogue plus a profile's overrides into something a keypress can
 * be looked up in.
 *
 * A keymap is built for one platform, which is injected rather than read from the
 * environment so a single test can build the Mac and the Windows map from the same
 * authored table and compare them. `mod` is resolved here and nowhere else: by the
 * time a binding reaches the dispatcher it says "meta" or "ctrl" and the hot path
 * is a Map lookup with no branching left in it.
 *
 * A command can hold two keys and both are always live -- there is no mode. The
 * slots exist so that a chord and a fast single key can be rebound independently,
 * which is also exactly the shape of the database: one row per command per slot.
 *
 * The awkward part is knowing when two bindings really collide. Notes binds "/"
 * to its search box and Gallery binds "/" to its own, and those are not in
 * conflict because only one screen is ever showing. That is what `VIEW_SCOPES`
 * buys: view tokens are mutually exclusive, so two bindings naming different
 * views can share a key, and two naming the same view (or neither) cannot.
 */

import {
  RESERVED_CHORDS,
  UNPREVENTABLE_CHORDS,
  formatSequenceKey,
  normalizeChord,
  parseSequence,
} from "./chord.js";
import { COMMANDS, VIEW_SCOPES, scopesOf } from "./commands.js";

/**
 * The two slots a command's keys live in, and the values stored in the database.
 * "primary" is the modifier chord, "alternate" the fast single key or sequence.
 * Both are always bound; neither is a mode.
 */
export const SLOTS = ["primary", "alternate"];

/**
 * Substitutes the platform's real modifier for `mod`. Always a modifier, so it is
 * always followed by a "+", which is what keeps this from touching a key named
 * after it.
 */
export function resolveMod(text, platform) {
  const token = platform === "mac" ? "meta" : "ctrl";
  return String(text ?? "").replace(/(^|\+)mod\+/g, `$1${token}+`);
}

/** How specific a scope list is. "global" is no condition, so it counts nothing. */
function specificityOf(scopes) {
  return scopes.filter((token) => token !== "global").length;
}

/**
 * Whether two scope lists can ever hold at the same time. Only view tokens can
 * make that impossible, because exactly one view is showing.
 */
export function scopesOverlap(a, b) {
  const viewA = a.find((token) => VIEW_SCOPES.has(token));
  const viewB = b.find((token) => VIEW_SCOPES.has(token));
  return !(viewA && viewB && viewA !== viewB);
}

/** Whether every token a binding requires is currently true. */
export function scopesSatisfied(scopes, active) {
  return scopes.every((token) => token === "global" || active.has(token));
}

function isStrictPrefix(shorter, longer) {
  return shorter.length < longer.length && shorter.every((chord, i) => chord === longer[i]);
}

/**
 * The full keymap for one platform and one mode.
 *
 * `overrides` is the rows as the API returns them; anything for the other keymap,
 * or for a command that no longer exists, is ignored rather than being an error --
 * a profile that has been through an upgrade will have both.
 */
export function buildKeymap(commands = COMMANDS, overrides = [], options = {}) {
  const { platform = "other" } = options;

  const overrideFor = new Map();
  for (const row of overrides ?? []) {
    if (typeof row?.command_id === "string" && SLOTS.includes(row.keymap)) {
      overrideFor.set(`${row.keymap}:${row.command_id}`, row.sequence ?? "");
    }
  }

  const bindings = [];
  let order = 0;
  for (const command of commands) {
    const scopes = scopesOf(command);
    for (const slot of SLOTS) {
      const authored = slot === "alternate" ? command.altKeys : command.keys;
      // A command with nothing authored for this slot has no binding in it --
      // rather than a second, identical copy of the other slot's.
      if (typeof authored !== "string" && !overrideFor.has(`${slot}:${command.id}`)) {
        continue;
      }
      const override = command.fixed ? undefined : overrideFor.get(`${slot}:${command.id}`);
      const chords = parseSequence(resolveMod(override ?? authored ?? "", platform));

      bindings.push({
        id: command.id,
        slot,
        chords,
        key: formatSequenceKey(chords),
        when: scopes,
        specificity: specificityOf(scopes),
        source: override === undefined ? "default" : "override",
        order: order += 1,
        repeatable: Boolean(command.repeatable),
        fixed: Boolean(command.fixed),
      });
    }
  }

  const bound = bindings.filter((binding) => binding.chords.length > 0 && !binding.fixed);

  const exact = new Map();
  for (const binding of bound) {
    const existing = exact.get(binding.key);
    if (existing) existing.push(binding);
    else exact.set(binding.key, [binding]);
  }
  // Most specific first, then a user's own choice over a shipped default, then
  // catalogue order so the result never depends on Map iteration.
  for (const list of exact.values()) {
    list.sort((a, b) =>
      b.specificity - a.specificity
      || (a.source === b.source ? 0 : a.source === "override" ? -1 : 1)
      || a.order - b.order);
  }

  const prefixes = new Set();
  for (const binding of bound) {
    for (let length = 1; length < binding.chords.length; length += 1) {
      prefixes.add(formatSequenceKey(binding.chords.slice(0, length)));
    }
  }

  return { platform, bindings, exact, prefixes };
}

/**
 * What a run of chords means right now.
 *
 * "run" carries the binding that won, "pending" means these chords start a longer
 * one, and "none" means they cannot become anything and the buffer should clear.
 * Pure, so the dispatcher's tests never need a timer or an event.
 */
export function matchSequence(keymap, chords, active = new Set()) {
  const key = formatSequenceKey(chords);

  for (const binding of keymap.exact.get(key) ?? []) {
    if (scopesSatisfied(binding.when, active)) {
      return { status: "run", binding };
    }
  }
  if (keymap.prefixes.has(key) && startsSomethingReachable(keymap, chords, active)) {
    return { status: "pending", binding: null };
  }
  return { status: "none", binding: null };
}

/**
 * Whether waiting on this prefix could still lead anywhere from here. Without it,
 * "g" would swallow the keystroke in a screen where every "g x" is out of scope
 * and leave the user pressing keys into a buffer that can never resolve.
 */
function startsSomethingReachable(keymap, chords, active) {
  for (const binding of keymap.bindings) {
    if (isStrictPrefix(chords, binding.chords) && scopesSatisfied(binding.when, active)) {
      return true;
    }
  }
  return false;
}

/**
 * The browser-owned chords, spelled for one platform. Exported because the
 * settings screen warns about them while a key is being recorded, before there is
 * a keymap to run `findConflicts` over.
 */
export function resolvedUnpreventable(platform) {
  return new Set([...UNPREVENTABLE_CHORDS].map((chord) => normalizeChord(resolveMod(chord, platform))));
}

/** Every binding for one command -- up to one per active slot. */
export function bindingsFor(keymap, id) {
  return keymap.bindings.filter((binding) => binding.id === id);
}

/**
 * The one binding to show where there is only room for one -- a palette row, say.
 * The fast key wins where it exists, being the shorter of the two.
 */
export function primaryBinding(keymap, id, slot) {
  const all = bindingsFor(keymap, id);
  if (slot) return all.find((binding) => binding.slot === slot);
  const bound = all.filter((binding) => binding.chords.length > 0);
  return bound.find((binding) => binding.slot === "alternate") ?? bound[0];
}

/**
 * Which commands a sequence would collide with if it were bound here, ignoring
 * the slot it is going into. Answered before anything is written, so the settings
 * screen can put the choice to the user rather than saving a broken keymap.
 */
export function wouldCollideWith(keymap, sequence, commandId, when = ["global"]) {
  const chords = parseSequence(resolveMod(sequence, keymap.platform));
  if (chords.length === 0) return [];
  const key = formatSequenceKey(chords);

  return (keymap.exact.get(key) ?? [])
    .filter((binding) => binding.id !== commandId && scopesOverlap(binding.when, when))
    .map((binding) => ({ id: binding.id, slot: binding.slot, key: binding.key }));
}

/**
 * Everything wrong with a keymap, as a flat list the settings screen can render.
 *
 * `duplicate` and `shadow` are structural and apply to defaults too -- they are
 * how the catalogue is stopped from rotting as commands are added. `reserved` and
 * `unpreventable` only ever fire on a user's own override, because the defaults
 * that use a reserved chord are the ones the engine implements directly.
 */
export function findConflicts(keymap) {
  const conflicts = [];
  const bound = keymap.bindings.filter((binding) => binding.chords.length > 0 && !binding.fixed);
  const unpreventable = resolvedUnpreventable(keymap.platform);

  // A command reached by the same key in both of its slots is not in conflict
  // with itself -- the key runs the one command either way. Saying "Settings
  // shares this key with Settings" would push somebody to fix a non-problem.
  for (const [key, list] of keymap.exact) {
    for (let i = 0; i < list.length; i += 1) {
      for (let j = i + 1; j < list.length; j += 1) {
        if (list[i].id !== list[j].id && scopesOverlap(list[i].when, list[j].when)) {
          conflicts.push({ kind: "duplicate", key, ids: [list[i].id, list[j].id] });
        }
      }
    }
  }

  for (const shorter of bound) {
    for (const longer of bound) {
      if (shorter.id === longer.id) continue;
      if (isStrictPrefix(shorter.chords, longer.chords) && scopesOverlap(shorter.when, longer.when)) {
        conflicts.push({ kind: "shadow", key: longer.key, ids: [shorter.id, longer.id] });
      }
    }
  }

  for (const binding of bound) {
    if (binding.source !== "override") continue;
    if (binding.chords.some((chord) => RESERVED_CHORDS.has(chord))) {
      conflicts.push({ kind: "reserved", key: binding.key, ids: [binding.id] });
    }
    if (binding.chords.some((chord) => unpreventable.has(chord))) {
      conflicts.push({ kind: "unpreventable", key: binding.key, ids: [binding.id] });
    }
  }

  return conflicts;
}
