import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import Icon from "../WorkspaceIcon.jsx";
import { registerModal } from "../modalStack.js";
import { useVisibleArea } from "./ui.jsx";
import { RULE_FIELDS, defaultRule, fieldSpec, opSpec, ruleIsComplete } from "./smartRules.js";

/** Pick several of a list (tags or folders), as toggles. */
function Choices({ options, value, onChange, empty }) {
  if (!options.length) return <p className="nw-smart-empty">{empty}</p>;
  const chosen = new Set(value ?? []);
  return (
    <div className="nw-smart-choices">
      {options.map((option) => (
        <button
          key={option.id}
          type="button"
          className={`nw-tag-chip ${chosen.has(option.id) ? "on" : ""}`.trim()}
          aria-pressed={chosen.has(option.id)}
          onClick={() => {
            const next = new Set(chosen);
            if (next.has(option.id)) next.delete(option.id);
            else next.add(option.id);
            onChange([...next]);
          }}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

function RuleValue({ rule, onChange, tags, folders }) {
  const spec = opSpec(rule.field, rule.op);
  if (spec.value === "tags") {
    return <Choices options={tags.map((tag) => ({ id: tag, label: `#${tag}` }))} value={rule.value} onChange={onChange} empty="No tags yet." />;
  }
  if (spec.value === "folders") {
    const options = [{ id: "default", label: "Notes" }, ...folders.map((folder) => ({ id: folder.id, label: folder.name }))];
    return <Choices options={options} value={rule.value} onChange={onChange} empty="" />;
  }
  if (spec.value === "days") {
    return (
      <label className="nw-smart-inline">
        <input type="number" min={1} max={3650} value={rule.value ?? 7} onChange={(event) => onChange(Number.parseInt(event.target.value, 10) || 1)} aria-label="Days" />
        <span>days</span>
      </label>
    );
  }
  if (spec.value === "date") {
    return <input type="date" value={rule.value ?? ""} onChange={(event) => onChange(event.target.value)} aria-label="Day" />;
  }
  if (spec.value === "bool") {
    return (
      <select value={rule.value ? "yes" : "no"} onChange={(event) => onChange(event.target.value === "yes")} aria-label="Yes or no">
        <option value="yes">Yes</option>
        <option value="no">No</option>
      </select>
    );
  }
  return null;
}

/**
 * Making or changing a smart folder: a name, and rules that all or any of
 * must match. Nothing is saved until every rule is complete.
 */
export default function SmartFolderDialog({ initial, tags, folders, onSubmit, onCancel }) {
  const [name, setName] = useState(initial?.name ?? "Smart Folder");
  const [match, setMatch] = useState(initial?.rules?.match ?? "all");
  const [rules, setRules] = useState(() => initial?.rules?.rules?.length ? initial.rules.rules : [defaultRule("tags", "includes_any")]);
  const nameInput = useRef(null);
  const scrim = useRef(null);
  useVisibleArea(scrim);

  useEffect(() => {
    const unregister = registerModal(onCancel);
    nameInput.current?.select();
    return unregister;
  }, [onCancel]);

  const update = (index, rule) => setRules((current) => current.map((each, at) => (at === index ? rule : each)));
  const complete = name.trim() && rules.length > 0 && rules.every(ruleIsComplete);

  return createPortal(
    <div ref={scrim} className="nw-scrim" onPointerDown={(event) => event.target === event.currentTarget && onCancel()}>
      <form
        className="nw-dialog nw-smart-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="nw-smart-title"
        onSubmit={(event) => {
          event.preventDefault();
          if (complete) onSubmit({ name: name.trim(), rules: { match, rules } });
        }}
      >
        <h2 id="nw-smart-title">{initial?.id ? "Edit Smart Folder" : "New Smart Folder"}</h2>
        <label className="nw-dialog-field">
          <span>Name</span>
          <input ref={nameInput} value={name} maxLength={100} onChange={(event) => setName(event.target.value)} />
        </label>
        <p className="nw-smart-match">
          Include notes matching{" "}
          <select value={match} onChange={(event) => setMatch(event.target.value)} aria-label="All or any">
            <option value="all">all</option>
            <option value="any">any</option>
          </select>{" "}
          of these:
        </p>
        <ol className="nw-smart-rules">
          {rules.map((rule, index) => {
            const spec = fieldSpec(rule.field);
            return (
              <li key={index} className="nw-smart-rule">
                <div className="nw-smart-row">
                  <select value={rule.field} onChange={(event) => update(index, defaultRule(event.target.value))} aria-label="Rule">
                    {RULE_FIELDS.map((field) => (
                      <option key={field.field} value={field.field}>{field.label}</option>
                    ))}
                  </select>
                  <select value={rule.op} onChange={(event) => update(index, defaultRule(rule.field, event.target.value))} aria-label="Condition">
                    {spec.ops.map((op) => (
                      <option key={op.op} value={op.op}>{op.label}</option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="nw-tool"
                    aria-label="Remove rule"
                    disabled={rules.length === 1}
                    onClick={() => setRules((current) => current.filter((_, at) => at !== index))}
                  >
                    <Icon name="close" />
                  </button>
                </div>
                <RuleValue rule={rule} tags={tags} folders={folders} onChange={(value) => update(index, { ...rule, value })} />
              </li>
            );
          })}
        </ol>
        <button type="button" className="nw-smart-add" onClick={() => setRules((current) => [...current, defaultRule("edited", "last_days")])}>
          <Icon name="plus" /> Add Rule
        </button>
        <div className="nw-dialog-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button type="submit" className="primary" disabled={!complete}>{initial?.id ? "Save" : "Create"}</button>
        </div>
      </form>
    </div>,
    document.body,
  );
}
