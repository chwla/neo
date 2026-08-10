import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api.js";

const SORT_OPTIONS = [
  ["newest", "Newest First"],
  ["oldest", "Oldest First"],
  ["az", "A → Z"],
];

// The six fields memory is organised into. The third entry is the storage type
// the API is given when a memory is created into that field.
const MEMORY_FIELDS = [
  ["profile", "Profile", "identity"],
  ["preferences", "Preferences", "preference"],
  ["goals", "Goals", "goal"],
  ["projects", "Projects", "project"],
  ["events", "Events", "event"],
  ["miscellaneous", "Miscellaneous", "knowledge"],
];

const MEMORY_TABS = MEMORY_FIELDS.map(([key, tabLabel]) => [key, tabLabel]);

const STORAGE_TYPE_FOR_FIELD = Object.fromEntries(
  MEMORY_FIELDS.map(([key, , storageType]) => [key, storageType]),
);

// Mirrors the server mapping so records saved before the API returned a field
// still land in the right tab.
const FIELD_FOR_TYPE = {
  identity: "profile",
  education: "profile",
  employment: "profile",
  preference: "preferences",
  goal: "goals",
  project: "projects",
  event: "events",
  activity: "events",
  knowledge: "miscellaneous",
};

function fieldOf(record) {
  return record.field || FIELD_FOR_TYPE[record.memory_type] || "miscellaneous";
}

function label(value) {
  return String(value || "")
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function timestamp(value) {
  const parsed = Date.parse(String(value || ""));
  return Number.isNaN(parsed) ? 0 : parsed;
}

function sortRecords(records, order) {
  return [...records].sort((left, right) => {
    if (order === "oldest") return timestamp(left.created_at) - timestamp(right.created_at);
    if (order === "az") {
      return String(left.display_text || left.canonical_value || "").localeCompare(
        String(right.display_text || right.canonical_value || ""),
        undefined,
        { sensitivity: "base" },
      );
    }
    return timestamp(right.created_at) - timestamp(left.created_at);
  });
}

function MemoryRecord({ record, refresh, reportError }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(record.display_text || "");
  const [saving, setSaving] = useState(false);

  useEffect(() => setValue(record.display_text || ""), [record.display_text]);

  async function save(event) {
    event.preventDefault();
    const cleaned = value.trim();
    if (!cleaned) return;
    setSaving(true);
    reportError("");
    try {
      await api.updateMemory(record.id, {
        value: cleaned,
        display_text: cleaned,
        expected_revision: record.revision,
      });
      setEditing(false);
      await refresh();
    } catch (error) {
      reportError(error.message || String(error));
    } finally {
      setSaving(false);
    }
  }

  async function forget() {
    setSaving(true);
    reportError("");
    try {
      await api.deleteMemory(record.id);
      await refresh();
    } catch (error) {
      reportError(error.message || String(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className={`memory-card ${editing ? "is-open" : ""}`} data-memory-id={record.id}>
      <div className="memory-card-head">
        <div>
          <p className="memory-card-summary">{record.display_text}</p>
          <div className="memory-provenance">
            <span>{label(fieldOf(record))}</span>
            <span>{record.scope?.type === "project" ? `Project: ${record.scope.project_name}` : "Available everywhere"}</span>
          </div>
        </div>
        <button
          className="memory-card-edit"
          type="button"
          aria-expanded={editing}
          onClick={() => setEditing((current) => !current)}
        >
          {editing ? "Cancel" : "Edit"}
        </button>
      </div>
      <p className="dialog-caption">
        Canonical value: {typeof record.canonical_value === "string"
          ? record.canonical_value
          : JSON.stringify(record.canonical_value)}
      </p>
      {editing && (
        <div className="memory-card-body">
          <form onSubmit={save}>
            <label className="memory-field">
              <span>Memory</span>
              <textarea value={value} onChange={(event) => setValue(event.target.value)} />
            </label>
            <div className="memory-actions">
              <button className="neo-button" type="submit" disabled={saving || !value.trim()}>
                {saving ? "Saving…" : "Save correction"}
              </button>
              <button className="neo-button danger" type="button" disabled={saving} onClick={forget}>
                Forget
              </button>
            </div>
          </form>
        </div>
      )}
    </section>
  );
}

function ManualMemory({ refresh, reportError, projects, field, onClose }) {
  const [value, setValue] = useState("");
  // A new memory defaults to the field being viewed, so adding from a tab keeps
  // the memory in that tab.
  const [memoryField, setMemoryField] = useState(field);
  const [scopeType, setScopeType] = useState(field === "projects" ? "project" : "global");
  const [projectId, setProjectId] = useState("");
  const [saving, setSaving] = useState(false);

  // Only the projects field is readable from a single project; every other
  // field is personal to the user and stays global.
  const projectScoped = memoryField === "projects";

  function changeField(next) {
    setMemoryField(next);
    setScopeType(next === "projects" ? "project" : "global");
  }

  async function save(event) {
    event.preventDefault();
    const cleaned = value.trim();
    if (!cleaned) return;
    setSaving(true);
    reportError("");
    try {
      await api.createMemory({
        value: cleaned,
        display_text: cleaned,
        memory_type: STORAGE_TYPE_FOR_FIELD[memoryField],
        scope_type: scopeType,
        project_id: scopeType === "project" ? projectId : null,
      });
      setValue("");
      await refresh();
      onClose();
    } catch (error) {
      reportError(error.message || String(error));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section className="memory-card is-open memory-add-card">
      <div className="memory-card-head">
        <p className="memory-card-summary">Add a memory</p>
        <button className="neo-button" type="button" onClick={onClose}>Cancel</button>
      </div>
      <div className="memory-card-body">
        <form onSubmit={save}>
          <label className="memory-field">
            <span>Memory</span>
            <textarea
              value={value}
              autoFocus
              onChange={(event) => setValue(event.target.value)}
            />
          </label>
          <label className="memory-field">
            <span>Field</span>
            <select value={memoryField} onChange={(event) => changeField(event.target.value)}>
              {MEMORY_FIELDS.map(([key, text]) => <option key={key} value={key}>{text}</option>)}
            </select>
          </label>
          {projectScoped ? (
            <label className="memory-field">
              <span>Project</span>
              <select
                value={projectId}
                onChange={(event) => setProjectId(event.target.value)}
                required
              >
                <option value="">Choose a project</option>
                {projects.map((project) => (
                  <option key={project.id} value={project.id}>{project.name}</option>
                ))}
              </select>
            </label>
          ) : (
            <p className="dialog-caption">Available in every chat and project.</p>
          )}
          <div className="memory-actions">
            <button
              className="neo-button"
              type="submit"
              disabled={saving || !value.trim() || (projectScoped && !projectId)}
            >
              {saving ? "Saving…" : "Add memory"}
            </button>
          </div>
        </form>
      </div>
    </section>
  );
}

export default function MemoryDialog({
  onClose,
}) {
  const [records, setRecords] = useState([]);
  const [projects, setProjects] = useState([]);
  const [activeTab, setActiveTab] = useState("profile");
  const [adding, setAdding] = useState(false);
  const [sortOrder, setSortOrder] = useState("newest");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [memoryRecords, chatProjects] = await Promise.all([api.memory(), api.chatProjects()]);
      setRecords(memoryRecords);
      setProjects(chatProjects);
    } catch (requestError) {
      setError(requestError.message || String(requestError));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);
  const visibleRecords = useMemo(
    () => sortRecords(
      records.filter((record) => activeTab === "projects"
        ? record.scope?.type === "project" || fieldOf(record) === "projects"
        : record.scope?.type !== "project" && fieldOf(record) === activeTab),
      sortOrder,
    ),
    [activeTab, records, sortOrder],
  );

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        className="neo-dialog neo-dialog-wide memory-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="memory-dialog-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="dialog-title-row">
          <h2 id="memory-dialog-title">Memories</h2>
          <button className="dialog-close" type="button" aria-label="Close" onClick={onClose}>×</button>
        </div>
        <div className="memory-tabs" role="tablist" aria-label="Memory sections">
          {MEMORY_TABS.map(([key, tabLabel]) => (
            <button
              key={key}
              className={key === activeTab ? "active" : ""}
              type="button"
              role="tab"
              aria-selected={key === activeTab}
              onClick={() => setActiveTab(key)}
            >
              {tabLabel}
            </button>
          ))}
        </div>
        {error && <div className="neo-error">{error}</div>}
        <div className="memory-sort-bar">
          <label>
            <span>Sort</span>
            <select value={sortOrder} onChange={(event) => setSortOrder(event.target.value)}>
              {SORT_OPTIONS.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
            </select>
          </label>
          <button className="neo-button" type="button" onClick={refresh} disabled={loading}>Refresh</button>
          <button
            className="neo-button memory-add-toggle"
            type="button"
            aria-expanded={adding}
            onClick={() => setAdding((open) => !open)}
          >
            + Add memory
          </button>
        </div>
        <div className="memory-scroll">
          {adding && (
            <ManualMemory
              key={activeTab}
              refresh={refresh}
              reportError={setError}
              projects={projects}
              field={activeTab}
              onClose={() => setAdding(false)}
            />
          )}
          {loading ? (
            <p className="dialog-caption">Loading memories…</p>
          ) : visibleRecords.length === 0 ? (
            <p className="dialog-caption">No saved memories</p>
          ) : activeTab === "projects" ? projects.map((project) => {
            const projectRecords = visibleRecords.filter((record) => record.scope?.project_id === String(project.id));
            return <section className="memory-project-group" key={project.id}><h3>{project.name}</h3>{projectRecords.length ? projectRecords.map((record) => <MemoryRecord key={record.id} record={record} refresh={refresh} reportError={setError} />) : <p className="dialog-caption">No private memories yet.</p>}</section>;
          }).concat((() => {
            // Project memories saved before scoping was applied have no project
            // attached, so surface them rather than letting them disappear.
            const unassigned = visibleRecords.filter((record) => record.scope?.type !== "project");
            if (!unassigned.length) return [];
            return [(
              <section className="memory-project-group" key="__unassigned">
                <h3>Not linked to a project</h3>
                {unassigned.map((record) => (
                  <MemoryRecord key={record.id} record={record} refresh={refresh} reportError={setError} />
                ))}
              </section>
            )];
          })()) : visibleRecords.map((record) => (
            <MemoryRecord key={record.id} record={record} refresh={refresh} reportError={setError} />
          ))}
        </div>
      </section>
    </div>
  );
}
