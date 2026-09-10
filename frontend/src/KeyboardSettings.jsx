import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";
import { formatSequence } from "./keys/chord.js";
import { COMMANDS, scopesOf } from "./keys/commands.js";
import { MAX_CAPTURE_CHORDS, createCaptureBuffer } from "./keys/capture.js";
import { resumeEngine, suspendEngine } from "./keys/engine.js";
import { SLOTS, buildKeymap, findConflicts, wouldCollideWith } from "./keys/keymap.js";

/** What each slot is called where a person has to read it. */
const SLOT_LABEL = { primary: "Shortcut", alternate: "Quick key" };

/** What each kind of conflict means, in the words the row says it in. */
const CONFLICT_TEXT = {
  duplicate: "shares this key with",
  shadow: "is unreachable because of",
  reserved: "uses a key the app needs for itself",
  unpreventable: "uses a key your browser takes first",
};

const titleOf = (id) => COMMANDS.find((command) => command.id === id)?.title ?? id;

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

/**
 * One command's row: what it does, and the one or two keys that run it.
 *
 * Both keys are live at once, so they are shown side by side rather than behind a
 * switch. The chord works wherever you are; the quick key only fires when you are
 * not typing, which is what makes a bare letter safe to offer at all.
 */
export function KeyboardRow({
  command, bindings, platform, conflict, recording, disabled = false, onRecord, onReset, onResolve,
}) {
  const label = (slot) => {
    const chords = bindings?.[slot]?.chords ?? [];
    return chords.length > 0 ? formatSequence(chords, platform) : "";
  };

  return (
    <div className={`kb-row${conflict ? " has-conflict" : ""}`}>
      <span className="kb-row-text">
        <strong>{command.title}</strong>
        <small>{command.section}</small>
      </span>

      {conflict ? (
        <span className="kb-row-conflict">
          {conflict.text}
          {conflict.resolvable && !disabled ? (
            <button className="kb-resolve" type="button" onClick={() => onResolve?.(conflict)}>
              Resolve
            </button>
          ) : null}
        </span>
      ) : null}

      {command.fixed ? (
        <span className="kb-row-fixed" title="Built in, and not rebindable">
          {label("primary") ? <kbd className="kb-kbd">{label("primary")}</kbd> : <small>Built in</small>}
        </span>
      ) : (
        <span className="kb-row-actions">
          {SLOTS.map((slot) => (
            <span className="kb-slot" key={slot}>
              {bindings?.[slot]?.source === "override" ? (
                <button
                  className="kb-reset"
                  type="button"
                  disabled={disabled}
                  title={`Put ${SLOT_LABEL[slot].toLowerCase()} back to its default`}
                  onClick={() => onReset?.(command.id, slot)}
                >
                  ↺
                </button>
              ) : null}
              <button
                className={`kb-record${recording === slot ? " is-recording" : ""}`
                  + (conflict && bindings?.[slot]?.key === conflict.key ? " is-clashing" : "")}
                type="button"
                disabled={disabled}
                aria-label={`${SLOT_LABEL[slot]} for ${command.title}`}
                title={SLOT_LABEL[slot]}
                onClick={() => onRecord?.(command.id, slot)}
              >
                {recording === slot ? "Press keys…" : label(slot) || "-"}
              </button>
            </span>
          ))}
        </span>
      )}
    </div>
  );
}

/**
 * Asks who keeps a key that two commands both want.
 *
 * Recording a key that is already taken is the one place where saving what was
 * asked for would quietly break something else, so it is the one place that stops
 * and asks. Giving the key away clears it from the command that had it, in the
 * same breath as setting it here -- otherwise "resolving" a conflict would leave
 * both commands on the key and the banner still up.
 */
export function ConflictDialog({ sequence, platform, claimant, holders, onKeep, onGiveAway, onCancel }) {
  const key = formatSequence(sequence, platform);
  return (
    <Modal title="That key is taken" onClose={onCancel} className="kb-conflict-dialog">
      <p className="dialog-caption">
        <kbd className="kb-kbd">{key}</kbd> already runs{" "}
        {holders.map((holder, index) => (
          <span key={holder.id}>
            {index > 0 ? ", " : ""}
            <strong>{titleOf(holder.id)}</strong>
          </span>
        ))}
        . One command can have it.
      </p>
      <div className="kb-conflict-choices">
        <button className="ws-save" type="button" onClick={onGiveAway}>
          Give it to {titleOf(claimant)}
          <small>
            {holders.map((holder) => titleOf(holder.id)).join(", ")}
            {holders.length === 1 ? " loses this key" : " lose this key"}
          </small>
        </button>
        <button type="button" onClick={onKeep}>
          Leave it with {holders.map((holder) => titleOf(holder.id)).join(", ")}
          <small>{titleOf(claimant)} keeps whatever it had</small>
        </button>
      </div>
      <div className="dialog-actions">
        <button type="button" onClick={onCancel}>Cancel</button>
      </div>
    </Modal>
  );
}

/**
 * The keyboard's own settings screen.
 *
 * Loads and saves the way every other API-backed panel here does -- a cancelled
 * flag on the effect, the server's answer taken as truth -- because every write
 * returns the whole configuration and re-syncing from it is one assignment.
 *
 * The engine is suspended while a key is being recorded. It is already inert
 * because this is a dialog and the engine stands down for those, but the capture
 * field must not depend on being inside one: recording "g c" while the engine is
 * live navigates you to the chat view halfway through the recording.
 */
export default function KeyboardSettings({ onClose, onConfigChange, platform = "other", backLabel, onBack }) {
  const [config, setConfig] = useState(null);
  //: { commandId, slot, chords, sequenceMode } while recording, else null.
  const [recording, setRecording] = useState(null);
  //: { sequence, commandId, slot, holders } while asking who keeps a key.
  const [contest, setContest] = useState(null);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const bufferRef = useRef(null);
  const captureRef = useRef(null);

  useEffect(() => {
    if (recording) captureRef.current?.focus();
  }, [recording?.commandId, recording?.slot, recording?.sequenceMode]);

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

  const keymap = useMemo(
    () => buildKeymap(COMMANDS, config?.overrides ?? [], { platform }),
    [config, platform],
  );

  const conflicts = useMemo(() => findConflicts(keymap), [keymap]);

  /* Worded from each row's own point of view: the row for A says it clashes with
     B, and B's row says it clashes with A. */
  const conflictFor = useMemo(() => {
    const byId = new Map();
    for (const entry of conflicts) {
      for (const id of entry.ids) {
        if (byId.has(id)) continue;
        const other = entry.ids.find((candidate) => candidate !== id);
        byId.set(id, {
          text: other && (entry.kind === "duplicate" || entry.kind === "shadow")
            ? `${CONFLICT_TEXT[entry.kind]} ${titleOf(other)}`
            : CONFLICT_TEXT[entry.kind],
          // Only a shared key is a question of who keeps it. A reserved chord has
          // no second claimant to hand it to; it just has to be changed.
          resolvable: entry.kind === "duplicate",
          key: entry.key,
          ids: entry.ids,
          claimant: id,
        });
      }
    }
    return byId;
  }, [conflicts]);

  /** Every binding a command has, keyed by slot, for the row to render. */
  const bindingsByCommand = useMemo(() => {
    const map = new Map();
    for (const binding of keymap.bindings) {
      const found = map.get(binding.id) ?? {};
      found[binding.slot] = binding;
      map.set(binding.id, found);
    }
    return map;
  }, [keymap]);

  async function save(request) {
    setBusy(true);
    setError("");
    const previous = config;
    try {
      const saved = await request();
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
  function startRecording(commandId, slot, sequenceMode = false) {
    suspendEngine();
    bufferRef.current = createCaptureBuffer({ maxChords: sequenceMode ? MAX_CAPTURE_CHORDS : 1 });
    setRecording({ commandId, slot, chords: [], sequenceMode });
  }

  function stopRecording() {
    resumeEngine();
    bufferRef.current = null;
    setRecording(null);
  }

  /**
   * Writes a key, unless somebody else already has it -- in which case the choice
   * goes to the user before anything is saved, rather than after.
   */
  function claim(commandId, slot, sequence) {
    stopRecording();
    if (sequence.length === 0) return;

    const command = COMMANDS.find((entry) => entry.id === commandId);
    const holders = wouldCollideWith(keymap, sequence, commandId, scopesOf(command ?? {}));
    if (holders.length > 0) {
      setContest({ sequence, commandId, slot, holders });
      return;
    }
    save(() => api.setKeybinding(slot, commandId, sequence));
  }

  /** Take the key: clear it from whoever had it, then set it here. */
  async function takeContestedKey() {
    const { sequence, commandId, slot, holders } = contest;
    setContest(null);
    await save(async () => {
      for (const holder of holders) {
        await api.setKeybinding(holder.slot, holder.id, "");
      }
      return api.setKeybinding(slot, commandId, sequence);
    });
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
      claim(recording.commandId, recording.slot, buffer.sequence);
      return;
    }
    const chords = buffer.chords;
    setRecording((current) => (current ? { ...current, chords } : current));
  }

  function commitRecording() {
    if (!recording || !bufferRef.current) {
      stopRecording();
      return;
    }
    claim(recording.commandId, recording.slot, bufferRef.current.sequence);
  }

  const disabled = !config || busy;
  const sections = sectionsFor(COMMANDS, filter);

  return (
    <Modal title="Keyboard" onClose={onClose} backLabel={backLabel} onBack={onBack}
      wide className="keyboard-dialog">
      <p className="dialog-caption">
        Every command can have a shortcut and a quick key. The shortcut works
        anywhere; the quick key only when you are not typing. Both are yours to
        change, and they are stored with this profile.
      </p>

      {error ? <div className="ws-error">{error}</div> : null}

      <div className="kb-toolbar">
        <input
          className="kb-filter"
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
          placeholder="Filter commands…"
          aria-label="Filter commands"
        />
        <button
          className="kb-reset-all"
          type="button"
          disabled={disabled}
          onClick={() => save(() => api.resetKeybindings())}
        >
          Reset all
        </button>
      </div>

      {conflicts.length > 0 ? (
        <div className="ws-error kb-banner">
          {conflicts.length === 1
            ? "One shortcut conflicts with another. Use Resolve to choose which keeps it."
            : `${conflicts.length} shortcuts conflict. Use Resolve to choose which keeps each key.`}
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
            <button
              type="button"
              onClick={() => startRecording(recording.commandId, recording.slot, true)}
            >
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
                bindings={bindingsByCommand.get(command.id)}
                platform={platform}
                conflict={conflictFor.get(command.id)}
                recording={recording?.commandId === command.id ? recording.slot : null}
                disabled={disabled}
                onRecord={startRecording}
                onReset={(id, slot) => save(() => api.clearKeybinding(slot, id))}
                onResolve={(entry) => setContest({
                  sequence: entry.key,
                  commandId: entry.claimant,
                  slot: bindingsByCommand.get(entry.claimant)?.alternate?.key === entry.key
                    ? "alternate" : "primary",
                  holders: entry.ids
                    .filter((id) => id !== entry.claimant)
                    .map((id) => ({
                      id,
                      slot: bindingsByCommand.get(id)?.alternate?.key === entry.key
                        ? "alternate" : "primary",
                      key: entry.key,
                    })),
                })}
              />
            ))}
          </section>
        ))
      )}

      {contest ? (
        <ConflictDialog
          sequence={contest.sequence}
          platform={platform}
          claimant={contest.commandId}
          holders={contest.holders}
          onGiveAway={takeContestedKey}
          onKeep={() => setContest(null)}
          onCancel={() => setContest(null)}
        />
      ) : null}
    </Modal>
  );
}
