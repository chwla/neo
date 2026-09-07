/**
 * Ranking commands for the palette.
 *
 * The palette is the answer to a real problem the app has independently of
 * keyboards: there are twelve screens and twenty-seven settings panels, and no
 * amount of rebinding helps somebody who does not know a thing exists. So the
 * ranking is tuned for recall rather than for search -- a short query should put
 * the obvious command first, and typing the initials of a two-word title should
 * find it.
 *
 * Scores are coarse bands rather than a continuous measure, so the order is easy
 * to reason about and a change to one band cannot quietly reshuffle another.
 */

import { scopesSatisfied } from "./keymap.js";
import { scopesOf } from "./commands.js";

const EXACT = 1000;
const PREFIX = 800;
const WORD_PREFIX = 700;
const CONTAINS = 600;
const KEYWORD = 400;
const SUBSEQUENCE = 200;

/** Whether every character of `query` appears in `text`, in order. */
function subsequenceScore(text, query) {
  let index = 0;
  let gaps = 0;
  let last = -1;
  for (const character of query) {
    const found = text.indexOf(character, index);
    if (found < 0) {
      return null;
    }
    if (last >= 0) gaps += found - last - 1;
    last = found;
    index = found + 1;
  }
  // A tighter run scores higher, so "gn" prefers "Go to Notes" over a title that
  // happens to contain a g and an n a long way apart.
  return SUBSEQUENCE - Math.min(gaps, SUBSEQUENCE - 1);
}

function scoreOne(command, query) {
  const title = command.title.toLowerCase();
  if (title === query) return EXACT;
  if (title.startsWith(query)) return PREFIX;
  if (title.split(/\s+/).some((word) => word.startsWith(query))) return WORD_PREFIX;
  if (title.includes(query)) return CONTAINS;

  const section = String(command.section ?? "").toLowerCase();
  if (section.startsWith(query)) return CONTAINS - 50;

  const keywords = String(command.keywords ?? "").toLowerCase();
  if (keywords.split(/\s+/).some((word) => word.startsWith(query))) return KEYWORD;

  return subsequenceScore(title, query);
}

/**
 * The commands worth offering for this query, best first.
 *
 * Commands are dropped when their screen is not showing, and `hidden` ones --
 * the scroll motions -- never appear at all: they are rebindable in the settings
 * screen, but "Scroll down" is not something anybody opens a palette to run.
 *
 * `isAvailable` lets the caller drop commands nothing has registered a handler
 * for, so the palette does not offer a row that would do nothing.
 */
export function rankCommands(commands, query, scopes = new Set(), options = {}) {
  const { isAvailable = () => true } = options;
  const needle = String(query ?? "").trim().toLowerCase();

  const eligible = commands.filter((command) =>
    !command.hidden
    && !command.fixed
    && scopesSatisfied(scopesOf(command), scopes)
    && isAvailable(command.id));

  if (needle === "") {
    return eligible;
  }

  return eligible
    .map((command, order) => ({ command, order, score: scoreOne(command, needle) }))
    .filter((entry) => entry.score !== null)
    // Catalogue order breaks ties, so the list never depends on sort stability.
    .sort((a, b) => b.score - a.score || a.order - b.order)
    .map((entry) => entry.command);
}
