import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";
import { formatSequence } from "./keys/chord.js";
import { COMMANDS } from "./keys/commands.js";
import { MAX_CAPTURE_CHORDS, createCaptureBuffer } from "./keys/capture.js";
import { resumeEngine, suspendEngine } from "./keys/engine.js";
import { buildKeymap, findConflicts, keymapName, primaryBinding } from "./keys/keymap.js";

/**
 * The commands to show, grouped under their section headings.
 *
 * With no filter the motions stay out of the way -- nobody scrolls a settings
 * screen looking for "Scroll down" -- but a search reaches them, so they are
 * rebindable by anybody who goes looking. Exported as the suite's way in: it
 * cannot press a key, so the parts worth pinning are the ones that are decisions.
 */
export function sectionsFor(commands, filter = "") {
  const needle = String(filter).trim().toLowerCase();
  const visible = commands.filter((command) => (needle === ""
    ? !command.hidden
    : command.title.toLowerCase().includes(needle)
      || command.section.toLowerCase().includes(needle)));

  const sections = [];
  for (const command of visible) {
    const last = sections[sections.length - 1];
    if (last && last.title === command.section) last.commands.push(command);
    else sections.push({ title: command.section, commands: [command] });
  }
  return sections;
}

/** What each kind of conflict means, in the words the row will say it in. */
const CONFLICT_TEXT = {
  duplicate: "shares this key with",
  shadow: "is unreachable because of",
  reserved: "uses a key the app needs for itself",
  unpreventable: "uses a key your browser takes first",
};

/** How a command's current binding reads, or the fact that it has none. */
function bindingLabel(binding, platform) {
  if (!binding || binding.chords.length === 0) {
    return "";
  }
  return formatSequence(binding.chords, platform);
}

/**
 * One command's row: what it does, what it is bound to, and the two things you
 * can do about it.
 *
 * Exported so the suite can render it with props. The tests cannot press a key,
 * so the parts worth pinning are the ones visible in the markup -- that a
 * rebound command offers a way back and a default one does not, and that a
 * conflict is stated on the row rather than only in the banner.
 */
export function KeyboardRow({
  command, binding, platform, conflict, recording, disabled = false, onRecord, onReset,
}) {
  const label = bindingLabel(binding, platform);
  const overridden = binding?.source === "override";

  return (
    <div className={`kb-row${conflict ? " has-conflict" : ""}`}>
      <span className="kb-row-text">
        <strong>{command.title}</strong>
        <small>{command.section}</small>
      </span>
      {conflict ? <span className="kb-row-conflict">{conflict}</span> : null}
      {command.fixed ? (
        <span className="kb-row-fixed" title="Built in, and not rebindable">
          {label ? <kbd className="kb-kbd">{label}</kbd> : <small>Built in</small>}
        </span>
      ) : (
        <span className="kb-row-actions">
          {overridden ? (
            <button
              className="kb-reset"
              type="button"
              disabled={disabled}
              onClick={() => onReset?.(command.id)}
            >
              Reset
            </button>
          ) : null}
          <button
            className={`kb-record${recording ? " is-recording" : ""}`}
            type="button"
            disabled={disabled}
            onClick={() => onRecord?.(command.id)}
          >
            {recording ? "Press keys…" : label || "Not bound"}
          </button>
        </span>
      )}
    </div>
  );
}

/**
 * The keyboard's own settings screen.
 *
 * Loads and saves the way every other API-backed panel here does -- a cancelled
 * flag on the effect, an optimistic write, the server's answer taken as truth --
 * because every write returns the whole configuration and re-syncing from it is
 * one assignment.
 *
 * The engine is suspended while a key is being recorded. It is already inert
 * because this is a dialog and the engine stands down for those, but the capture
 * field must not depend on being inside one: recording "g c" while the engine is
 * live navigates you to the chat view halfway through the recording.
 */
export default function KeyboardSettings({ onClose, onConfigChange, platform = "other" }) {
  const [config, setConfig] = useState(null);
  const [editing, setEditing] = useState(null);
  //: { commandId, chords, sequenceMode } while a key is being recorded, else null.
  const [recording, setRecording] = useState(null);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const bufferRef = useRef(null);
  const captureRef = useRef(null);

  // The panel only receives keys while it has focus, and it appears under the
  // pointer rather than at it, so focus has to be moved deliberately.
  useEffect(() => {
    if (recording) captureRef.current?.focus();
  }, [recording?.commandId, recording?.sequenceMode]);

  useEffect(() => {
    let cancelled = false;
    api.keyboardConfig()
      .then((data) => { if (!cancelled) { setConfig(data); onConfigChange?.(data); } })
      .catch(() => {
        if (!cancelled) {
          setError("Could not reach the server, so these are the shipped defaults."
            + " They are still live -- but changes cannot be saved until it is back.");
        }
      });
    return () => { cancelled = true; };
  }, []);

  // Which keymap is being edited. Follows the toggle on arrival, then stays where
  // the user put it -- editing the standard keymap with Command mode on is a
  // thing people will want to do.
  const active = editing ?? keymapName(Boolean(config?.command_mode_enabled));

  // Always built with both slots live, whatever the toggle says, because this is
  // the screen where the other one is edited. Which slot a row shows is then the
  // segmented control's business, not the keymap's.
  const keymap = useMemo(
    () => buildKeymap(COMMANDS, config?.overrides ?? [], { platform, commandMode: true }),
    [config, platform],
  );

  const conflicts = useMemo(() => findConflicts(keymap), [keymap]);

  /* Worded from each row's own point of view: the row for A says it clashes with
     B, and B's row says it clashes with A. Naming the same command on both rows
     -- which is what taking `ids[1]` for everybody did -- reads as a command
     conflicting with itself. */
  const conflictFor = useMemo(() => {
    const byId = new Map();
    const titleOf = (id) => COMMANDS.find((command) => command.id === id)?.title ?? id;

    for (const entry of conflicts) {
      for (const id of entry.ids) {
        if (byId.has(id)) continue;
        const other = entry.ids.find((candidate) => candidate !== id);
        byId.set(id, other && (entry.kind === "duplicate" || entry.kind === "shadow")
          ? `${CONFLICT_TEXT[entry.kind]} ${titleOf(other)}`
          : CONFLICT_TEXT[entry.kind]);
      }
    }
    return byId;
  }, [conflicts]);

  /**
   * Every write answers with the whole configuration, so reconciling is one
   * assignment -- and the same answer is handed up so the live keymap is rebuilt
   * from it rather than from a second request.
   */
  async function save(next) {
    setBusy(true);
    setError("");
    const previous = config;
    if (next.optimistic) setConfig(next.optimistic);
    try {
      const saved = await next.request();
      setConfig(saved);
      onConfigChange?.(saved);
    } catch {
      setConfig(previous);
      onConfigChange?.(previous);
      setError("Could not save that. Your shortcuts are unchanged.");
    } finally {
      setBusy(false);
    }
  }

  /* One key is what almost every rebinding is, so that is what recording does by
     default and it commits the moment the key lands -- no confirmation step for
     the common case. A sequence has no natural end (is "g" the whole binding, or
     the start of "g c"?) and no timeout tells them apart, so it is a deliberate
     mode with a Done button rather than a guess. */
  function startRecording(commandId, sequenceMode = false) {
    suspendEngine();
    bufferRef.current = createCaptureBuffer({
      maxChords: sequenceMode ? MAX_CAPTURE_CHORDS : 1,
    });
    setRecording({ commandId, chords: [], sequenceMode });
  }

  function stopRecording() {
    resumeEngine();
    bufferRef.current = null;
    setRecording(null);
  }

  function commit(commandId, sequence) {
    stopRecording();
    if (sequence.length === 0) return;
    save({ request: () => api.setKeybinding(active, commandId, sequence) });
  }

  function onCaptureKeyDown(event) {
    const buffer = bufferRef.current;
    if (!recording || !buffer) return;
    // Everything, unconditionally: a field that is recording keys must not also
    // let them do their usual jobs while it does.
    event.preventDefault();
    event.stopPropagation();

    const outcome = buffer.push(event);
    if (outcome === "cancelled") {
      stopRecording();
      return;
    }
    if (outcome === "ignored") return;

    if (outcome === "full") {
      commit(recording.commandId, buffer.sequence);
      return;
    }
    // Shown as they land, so a part-typed sequence is visible before it is saved.
    const chords = buffer.chords;
    setRecording((current) => (current ? { ...current, chords } : current));
  }

  function commitRecording() {
    if (!recording || !bufferRef.current) {
      stopRecording();
      return;
    }
    commit(recording.commandId, bufferRef.current.sequence);
  }

  function resetOne(commandId) {
    save({ request: () => api.clearKeybinding(active, commandId) });
  }

  function resetAll() {
    save({ request: () => api.resetKeybindings() });
  }

  function toggleCommandMode(enabled) {
    save({
      optimistic: config ? { ...config, command_mode_enabled: enabled } : config,
      request: () => api.updateKeyboardConfig({ command_mode_enabled: enabled }),
    });
  }

  const sections = sectionsFor(COMMANDS, filter);

  return (
    <Modal title="Keyboard" onClose={onClose} wide className="keyboard-dialog">
      <p className="dialog-caption">
        Shortcuts are stored with this profile, so they follow you between browsers.
      </p>

      {error ? <div className="ws-error">{error}</div> : null}

      <label className="kb-toggle">
        <input
          type="checkbox"
          checked={Boolean(config?.command_mode_enabled)}
          disabled={!config || busy}
          onChange={(event) => toggleCommandMode(event.target.checked)}
        />
        <span>
          <strong>Command mode</strong>
          <small>
            Single keys run commands whenever you are not typing. Escape leaves the
            composer; <kbd className="kb-kbd">i</kbd> goes back to it.
          </small>
        </span>
      </label>

      <div className="kb-toolbar">
        <div className="kb-which" role="group" aria-label="Which shortcuts to edit">
          <button
            type="button"
            className={active === "standard" ? "is-active" : ""}
            onClick={() => setEditing("standard")}
          >
            Always on
          </button>
          <button
            type="button"
            className={active === "command" ? "is-active" : ""}
            onClick={() => setEditing("command")}
          >
            Command mode
          </button>
        </div>
        <input
          className="kb-filter"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="Filter commands…"
          aria-label="Filter commands"
        />
        <button className="kb-reset-all" type="button" onClick={resetAll} disabled={!config || busy}>
          Reset all
        </button>
      </div>

      {conflicts.length > 0 ? (
        <div className="ws-error kb-banner">
          {conflicts.length === 1
            ? "One shortcut conflicts with another."
            : `${conflicts.length} shortcuts conflict with others.`}
        </div>
      ) : null}

      {recording ? (
        <div
          className="kb-recording"
          ref={captureRef}
          onKeyDown={onCaptureKeyDown}
          tabIndex={-1}
          role="status"
        >
          <span>
            {recording.chords.length > 0
              ? <kbd className="kb-kbd">{formatSequence(recording.chords, platform)}</kbd>
              : (recording.sequenceMode ? "Press up to three keys in a row." : "Press a key.")}
            {" "}Escape cancels.
          </span>
          {recording.sequenceMode ? (
            <button type="button" onClick={commitRecording}>Done</button>
          ) : (
            <button type="button" onClick={() => startRecording(recording.commandId, true)}>
              Record a sequence
            </button>
          )}
        </div>
      ) : null}

      {config === null && !error ? (
        <p className="dialog-caption">Loading your shortcuts…</p>
      ) : (
        sections.map((section) => (
          <section className="kb-section" key={section.title}>
            <h3>{section.title}</h3>
            {section.commands.map((command) => (
              <KeyboardRow
                key={command.id}
                command={command}
                binding={primaryBinding(keymap, command.id, command.fixed ? undefined : active)}
                platform={platform}
                conflict={conflictFor.get(command.id)}
                recording={recording?.commandId === command.id}
                disabled={!config || busy}
                onRecord={startRecording}
                onReset={resetOne}
              />
            ))}
          </section>
        ))
      )}
    </Modal>
  );
}
