import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";

/**
 * Choosing the folder Agent Mode will edit, by browsing the real filesystem.
 *
 * Two things this deliberately does not do. It does not compute the breadcrumb:
 * the server knows that ``/workspace/Desktop`` is really ``~/Desktop`` and this
 * side does not, so the trail arrives ready to render. And it never sends a
 * displayed path back -- every request carries ``entry.path``, the container
 * path, so what the user reads and what validation receives cannot diverge.
 *
 * Descending and selecting are separate controls rather than the same click on
 * different rows: the name navigates, the Select button opens that folder as the
 * workspace.
 */
export default function OpenFolderDialog({ projectId, onClose, onAttached }) {
  const [view, setView] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");

  const load = useCallback(async (path) => {
    setLoading(true);
    setError("");
    try {
      const next = await api.browseFolders(path);
      setView(next);
      setFilter("");
    } catch (browseError) {
      setError(browseError.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load(null);
  }, [load]);

  const entries = view?.entries || [];
  const visible = useMemo(() => {
    const needle = filter.trim().toLowerCase();
    if (!needle) return entries;
    return entries.filter((entry) => entry.name.toLowerCase().includes(needle));
  }, [entries, filter]);

  /** Open `path` as the workspace. Always a container path, never a display one. */
  async function select(path) {
    setBusy(true);
    setError("");
    try {
      const result = await api.attachFolder({ path, projectId });
      onAttached(result.repo, result.stats);
      onClose();
    } catch (attachError) {
      setError(attachError.message);
      setBusy(false);
    }
  }

  const trail = view?.trail || [];

  return (
    <Modal title="Open a folder" onClose={onClose} className="open-folder">
      <div className="open-folder-body">
        {trail.length ? (
          <nav className="open-folder-trail" aria-label="Folder path">
            {trail.map((segment, index) => (
              <span key={segment.path} className="open-folder-crumb">
                {index ? <span className="open-folder-crumb-sep">›</span> : null}
                <button
                  type="button"
                  disabled={busy || segment.path === view?.path}
                  onClick={() => load(segment.path)}
                >
                  {segment.label}
                </button>
              </span>
            ))}
          </nav>
        ) : null}

        {loading ? (
          <p className="open-folder-empty">Loading folders…</p>
        ) : (
          <>
            {entries.length > 8 ? (
              <input
                className="open-folder-filter"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                placeholder="Filter folders"
                aria-label="Filter folders"
              />
            ) : null}

            {view?.parent || entries.length ? (
              <ul className="open-folder-list">
                {view?.parent ? (
                  <li>
                    <button
                      type="button"
                      className="open-folder-item up"
                      disabled={busy}
                      onClick={() => load(view.parent)}
                    >
                      <span className="open-folder-item-name">↑ Up one level</span>
                    </button>
                  </li>
                ) : null}
                {visible.map((entry) => (
                  <li key={entry.path} className="open-folder-row">
                    <button
                      type="button"
                      className="open-folder-item"
                      disabled={busy}
                      title={entry.display_path || entry.path}
                      onClick={() => load(entry.path)}
                    >
                      <span className="open-folder-item-name">{entry.name}</span>
                      <span className="open-folder-item-meta">
                        {entry.is_git ? <span className="open-folder-tag">git</span> : null}
                        {entry.attached_repo_id ? (
                          <span className="open-folder-tag muted">open</span>
                        ) : null}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="open-folder-select"
                      disabled={busy}
                      title={`Open ${entry.display_path || entry.path} as the workspace`}
                      onClick={() => select(entry.path)}
                    >
                      Select
                    </button>
                  </li>
                ))}
                {entries.length && !visible.length ? (
                  <li className="open-folder-empty">Nothing matches “{filter}”.</li>
                ) : null}
              </ul>
            ) : (
              <NothingHere displayPath={view?.display_path} containerized={view?.containerized} />
            )}
          </>
        )}

        {error ? <div className="open-folder-error">{error}</div> : null}

        <div className="open-folder-actions">
          {view?.display_path ? (
            <p className="open-folder-selected">
              Current folder: <code>{view.display_path}</code>
            </p>
          ) : null}
          {view?.attach_blocked_reason && !error ? (
            <p className="open-folder-note">{view.attach_blocked_reason}</p>
          ) : null}
          <button
            className="ws-primary"
            type="button"
            onClick={() => select(view.path)}
            disabled={busy || loading || !view?.can_attach}
          >
            {busy ? "Opening…" : "Open this folder"}
          </button>
        </div>
      </div>
    </Modal>
  );
}

/**
 * A directory with nothing in it. Inside a container that is usually a mount that
 * was never pointed anywhere, which is a settings problem rather than a
 * navigation one -- so this names the setting instead of leaving a blank panel.
 */
function NothingHere({ displayPath, containerized }) {
  if (!containerized) {
    return <p className="open-folder-empty">This folder has no subfolders.</p>;
  }
  return (
    <div className="open-folder-empty-state">
      <p>
        {displayPath ? (
          <>
            <code>{displayPath}</code> is mounted, but there is nothing inside it.
          </>
        ) : (
          <>No folder is mounted into the container yet.</>
        )}
      </p>
      <p className="open-folder-note">
        Neo runs in a container and can only reach folders mounted into it. Point{" "}
        <code>NEO_WORKSPACE_HOST_ROOT</code> at the folder that holds your projects in the{" "}
        <code>.env</code> next to <code>docker-compose.yml</code>, then run{" "}
        <code>docker compose up -d</code>.
      </p>
    </div>
  );
}
