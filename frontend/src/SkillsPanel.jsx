import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";
import FolderBrowser from "./FolderBrowser.jsx";

/**
 * The skills library, and which of them this chat may use.
 *
 * A skill is a folder holding SKILL.md: frontmatter naming it and saying when
 * it applies, then instructions an agent run loads when it decides the skill is
 * relevant. Installing one is global; turning one on or off is per chat.
 *
 * That split is why each row has one switch and not two. The switch is *this
 * chat*, because that is the decision someone opening this panel came to make.
 * The library-wide default sits under Options, one disclosure away, because it
 * is a decision about future chats and putting it in the row would leave two
 * switches with no way to tell which was which.
 *
 * An override is stored only when it differs from the skill's default, so
 * "reset" is simply an empty map -- and a chat that never had an opinion keeps
 * following the library as the library changes.
 */
export default function SkillsPanel({ chatId, onClose }) {
  const [skills, setSkills] = useState([]);
  const [overrides, setOverrides] = useState({});
  const [loading, setLoading] = useState(true);
  const [notice, setNotice] = useState(null);
  const [busySlug, setBusySlug] = useState(null);
  const [adding, setAdding] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const result = await api.skills(chatId);
      setSkills(result.skills || []);
      setOverrides(result.overrides || {});
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    } finally {
      setLoading(false);
    }
  }, [chatId]);

  useEffect(() => {
    load();
  }, [load]);

  async function writeOverrides(next) {
    if (!chatId) {
      // No conversation to hang the decision on yet. Saying so beats a switch
      // that moves and then silently does nothing on the next turn.
      setNotice({ type: "error", text: "Start a chat before turning skills on or off for it." });
      return;
    }
    setNotice(null);
    try {
      await api.updateChat(chatId, { skill_overrides: next });
      await load();
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    }
  }

  async function toggle(skill) {
    setBusySlug(skill.slug);
    const next = { ...overrides };
    if (!skill.enabled === skill.enabled_by_default) {
      // Back in line with the default, so stop recording an opinion at all.
      delete next[skill.slug];
    } else {
      next[skill.slug] = !skill.enabled;
    }
    await writeOverrides(next);
    setBusySlug(null);
  }

  async function setDefault(skill, value) {
    setBusySlug(skill.slug);
    setNotice(null);
    try {
      await api.updateSkill(skill.id, { enabled_by_default: value });
      await load();
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    } finally {
      setBusySlug(null);
    }
  }

  async function remove(skill) {
    setBusySlug(skill.slug);
    setNotice(null);
    try {
      await api.removeSkill(skill.id);
      await load();
    } catch (error) {
      setNotice({ type: "error", text: error.message });
    } finally {
      setBusySlug(null);
    }
  }

  if (adding) {
    return (
      <AddSkill
        onClose={onClose}
        onCancel={() => setAdding(false)}
        onAdded={async () => {
          setAdding(false);
          await load();
        }}
      />
    );
  }

  const customised = Object.keys(overrides).length > 0;

  return (
    <Modal title="Skills" onClose={onClose} wide className="chat-tools-panel skills-panel">
      {notice ? <div className={`connector-notice ${notice.type}`}>{notice.text}</div> : null}

      <div className="chat-tools-toolbar">
        <p>
          Instructions an agent run can load when they apply. Turning one off here affects this
          chat only.
        </p>
        <div className="skills-toolbar-actions">
          {customised ? (
            <button type="button" className="skills-reset" onClick={() => writeOverrides({})}>
              Reset to defaults
            </button>
          ) : null}
          <button type="button" className="ws-primary" onClick={() => setAdding(true)}>
            Add skill
          </button>
        </div>
      </div>

      {loading ? (
        <p className="open-folder-empty">Loading skills…</p>
      ) : skills.length === 0 ? (
        <div className="open-folder-empty-state">
          <p>No skills yet.</p>
          <p className="open-folder-note">
            A skill is a folder with a <code>SKILL.md</code> in it: a name, a line saying when to
            use it, and the instructions to follow. Add one from a folder on this computer, from
            GitHub, or write one here.
          </p>
        </div>
      ) : (
        <ul className="chat-tools-list">
          {skills.map((skill) => (
            <li key={skill.slug} className="chat-tools-row skills-row">
              <div className="chat-tools-row-info">
                <div className="chat-tools-row-title">
                  <strong>{skill.name}</strong>
                </div>
                <p>{skill.description}</p>
                <details className="skills-options">
                  <summary>Options</summary>
                  <div className="skills-options-body">
                    <label className="skills-default">
                      <input
                        type="checkbox"
                        checked={skill.enabled_by_default}
                        disabled={busySlug === skill.slug}
                        onChange={() => setDefault(skill, !skill.enabled_by_default)}
                      />
                      <span>On by default in new chats</span>
                    </label>
                    <p className="skills-source">
                      {skill.source_type === "github"
                        ? `From GitHub: ${skill.source_ref}`
                        : skill.source_type === "folder"
                          ? `From ${skill.source_ref}`
                          : "Written here"}
                    </p>
                    <button
                      type="button"
                      className="skills-remove"
                      disabled={busySlug === skill.slug}
                      onClick={() => remove(skill)}
                    >
                      Remove skill
                    </button>
                  </div>
                </details>
              </div>
              <label className="chat-tools-toggle">
                <input
                  type="checkbox"
                  checked={skill.enabled}
                  disabled={busySlug === skill.slug}
                  onChange={() => toggle(skill)}
                  aria-label={`${skill.enabled ? "Disable" : "Enable"} ${skill.name} for this chat`}
                />
              </label>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

const SOURCES = [
  { key: "folder", label: "From a folder" },
  { key: "github", label: "From GitHub" },
  { key: "write", label: "Write one" },
];

/**
 * The three ways a skill arrives, behind one back button.
 *
 * Same modal rather than a second one stacked on the first: adding a skill is a
 * step inside managing them, and a dialog over a dialog would make Escape mean
 * two different things depending on how far in you were.
 */
function AddSkill({ onClose, onCancel, onAdded }) {
  const [source, setSource] = useState("folder");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");

  async function run(work) {
    setBusy(true);
    setError("");
    try {
      await work();
      await onAdded();
    } catch (addError) {
      setError(addError.message);
      setBusy(false);
    }
  }

  return (
    <Modal
      title="Add a skill"
      onClose={onClose}
      wide
      className="chat-tools-panel skills-panel"
      backLabel="Skills"
      onBack={onCancel}
    >
      <div className="skills-sources" role="tablist" aria-label="Where the skill comes from">
        {SOURCES.map((option) => (
          <button
            key={option.key}
            type="button"
            role="tab"
            aria-selected={source === option.key}
            className={source === option.key ? "active" : ""}
            disabled={busy}
            onClick={() => {
              setSource(option.key);
              setError("");
            }}
          >
            {option.label}
          </button>
        ))}
      </div>

      {source === "folder" ? (
        <div className="open-folder-body">
          <p className="open-folder-note">
            Pick the folder that holds the skill&apos;s <code>SKILL.md</code>. Neo copies it in, so
            moving or deleting the original later will not change how the skill behaves.
          </p>
          <FolderBrowser
            busy={busy}
            selectLabel="Add"
            selectTitle={(entry) => `Install ${entry.display_path || entry.path} as a skill`}
            onSelect={(path) => run(() => api.installSkillFromFolder(path))}
          />
        </div>
      ) : null}

      {source === "github" ? (
        <form
          className="skills-form"
          onSubmit={(event) => {
            event.preventDefault();
            run(() => api.installSkillFromGithub(url.trim()));
          }}
        >
          <label>
            <span>GitHub folder</span>
            <input
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://github.com/owner/repo/tree/main/skills/my-skill"
              aria-label="GitHub folder URL"
            />
          </label>
          <p className="open-folder-note">
            The folder that holds <code>SKILL.md</code>, not the file itself. Its instructions are
            guidance for the model, and never widen what a run is allowed to do.
          </p>
          <button className="ws-primary" type="submit" disabled={busy || !url.trim()}>
            {busy ? "Fetching…" : "Add skill"}
          </button>
        </form>
      ) : null}

      {source === "write" ? (
        <form
          className="skills-form"
          onSubmit={(event) => {
            event.preventDefault();
            run(() =>
              api.createSkill({
                name: name.trim(),
                description: description.trim(),
                instructions: instructions.trim(),
              }),
            );
          }}
        >
          <label>
            <span>Name</span>
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Release notes"
              aria-label="Skill name"
            />
          </label>
          <label>
            <span>When to use it</span>
            <input
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="Use when the user asks for release notes from a diff."
              aria-label="When to use this skill"
            />
          </label>
          <p className="open-folder-note">
            This line is all the agent sees until it decides the skill applies, so say when to
            reach for it rather than what it contains.
          </p>
          <label>
            <span>Instructions</span>
            <textarea
              rows={10}
              value={instructions}
              onChange={(event) => setInstructions(event.target.value)}
              placeholder="Group changes by user-visible effect. Lead with what broke…"
              aria-label="Skill instructions"
            />
          </label>
          <button
            className="ws-primary"
            type="submit"
            disabled={busy || !name.trim() || !description.trim() || !instructions.trim()}
          >
            {busy ? "Saving…" : "Add skill"}
          </button>
        </form>
      ) : null}

      {error ? <div className="open-folder-error">{error}</div> : null}
    </Modal>
  );
}
