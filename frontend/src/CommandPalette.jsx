import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { formatSequence } from "./keys/chord.js";
import { COMMANDS } from "./keys/commands.js";
import { primaryBinding } from "./keys/keymap.js";
import { rankCommands } from "./keys/paletteSearch.js";
import { hasHandler, runCommand } from "./keys/registry.js";
import { registerModal } from "./modalStack.js";

/**
 * Everything the keyboard can do, searchable.
 *
 * The palette earns its place independently of the keyboard: the app has twelve
 * screens and twenty-seven settings panels, and no amount of rebinding helps
 * somebody who does not know a panel exists. It is also the only way to reach the
 * handful of commands that ship with no key at all.
 *
 * Rows nothing has registered a handler for are left out rather than greyed:
 * offering a row that does nothing is worse than a shorter list.
 *
 * Its own arrow keys are on the input rather than going through the engine,
 * because registering as a dialog makes the engine stand down -- which is what
 * keeps a "j" typed into the search box from scrolling the transcript behind it.
 */
export default function CommandPalette({
  keymap,
  scopes,
  platform = "other",
  commands = COMMANDS,
  isAvailable = hasHandler,
  onClose,
}) {
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState(0);
  const inputRef = useRef(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => registerModal(() => closeRef.current?.()), []);
  useEffect(() => { inputRef.current?.focus(); }, []);

  const results = useMemo(
    () => rankCommands(commands, query, scopes, { isAvailable }),
    [commands, query, scopes, isAvailable],
  );

  // A new query means a new list, and keeping the old index would leave the
  // highlight on whatever happened to land in that row.
  const active = Math.min(selected, Math.max(results.length - 1, 0));

  function move(delta) {
    if (results.length === 0) return;
    setSelected((current) => {
      const next = Math.min(current, results.length - 1) + delta;
      return (next + results.length) % results.length;
    });
  }

  function choose(command) {
    if (!command) return;
    onClose?.();
    // After the close, so a command that opens another dialog is not immediately
    // unwound by this one closing.
    runCommand(command.id, { count: 1, sequence: "", event: null });
  }

  function onKeyDown(event) {
    const control = event.ctrlKey && !event.metaKey && !event.altKey;
    if (event.key === "ArrowDown" || (control && event.key === "n")) {
      event.preventDefault();
      move(1);
    } else if (event.key === "ArrowUp" || (control && event.key === "p")) {
      event.preventDefault();
      move(-1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(results[active]);
    }
  }

  function bindingLabel(id) {
    const chords = keymap ? primaryBinding(keymap, id)?.chords ?? [] : [];
    return chords.length > 0 ? formatSequence(chords, platform) : "";
  }

  const palette = (
    <div className="modal-backdrop cmdk-backdrop" role="presentation" onMouseDown={onClose}>
      <div
        className="cmdk"
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <input
          ref={inputRef}
          className="cmdk-input"
          value={query}
          onChange={(event) => { setQuery(event.target.value); setSelected(0); }}
          onKeyDown={onKeyDown}
          placeholder="Search commands…"
          aria-label="Search commands"
          autoComplete="off"
          spellCheck={false}
        />
        {results.length === 0 ? (
          <p className="cmdk-empty">No command matches that.</p>
        ) : (
          <ul className="cmdk-list" role="listbox" aria-label="Commands">
            {results.map((command, index) => {
              const binding = bindingLabel(command.id);
              return (
                <li key={command.id}>
                  <button
                    type="button"
                    className={`cmdk-row${index === active ? " is-active" : ""}`}
                    role="option"
                    aria-selected={index === active}
                    onMouseDown={(event) => event.preventDefault()}
                    onClick={() => choose(command)}
                  >
                    <span className="cmdk-row-text">
                      <strong>{command.title}</strong>
                      <small>{command.section}</small>
                    </span>
                    {binding ? <kbd className="cmdk-kbd">{binding}</kbd> : null}
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );

  // Portalled for the same reason every other dialog is: the composer sets
  // backdrop-filter, which makes it the containing block for anything fixed
  // inside it.
  return typeof document === "undefined" ? palette : createPortal(palette, document.body);
}
