import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";

/**
 * Choosing the folder Agent Mode will edit, by browsing rather than by typing.
 *
 * The typed path this replaces asked a question the user could not answer:
 * inside a container the only real paths are the mounted ones, and nothing in
 * the browser reveals them, so a typo and an unmounted folder looked identical.
 * The server knows the configured roots and owns containment, so it lists what
 * is reachable one level at a time and this walks that listing.
 *
 * Opening always attaches live -- the agent edits the folder in place. The
 * server decides whether the current folder may be opened at all; a configured
 * root is browse-only, and that arrives here as `can_attach` with a reason
 * rather than being re-derived on this side.
 */

/**
 * The clickable path trail, cut off at the configured root.
 *
 * Never yields a segment above the root: those are directories the browser is
 * not permitted to list, so offering them would build a control that can only
 * produce an error.
 */
export function breadcrumbSegments(cwd, roots = []) {
  if (!cwd) return [];
  const sep = cwd.includes("\\") && !cwd.includes("/") ? "\\" : "/";
  const containing = (roots || [])
    .filter((root) => cwd === root || cwd.startsWith(root.endsWith(sep) ? root : root + sep))
    .sort((a, b) => b.length - a.length)[0];
  if (!containing) return [{ label: cwd, path: cwd }];

  const trail = [{ label: containing, path: containing }];
  let walked = containing.endsWith(sep) ? containing.slice(0, -1) : containing;
  for (const name of cwd.slice(containing.length).split(sep).filter(Boolean)) {
    walked = `${walked}${sep}${name}`;
    trail.push({ label: name, path: walked });
  }
  return trail;
}

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

  const trail = useMemo(() => breadcrumbSegments(view?.path, view?.roots), [view]);

  async function attach() {
    if (!view?.path) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.attachFolder({ path: view.path, projectId });
      onAttached(result.repo, result.stats);
      onClose();
    } catch (attachError) {
      setError(attachError.message);
      setBusy(false);
    }
  }

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
                  <li key={entry.path}>
                    <button
                      type="button"
                      className="open-folder-item"
                      disabled={busy}
                      title={entry.path}
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
                  </li>
                ))}
                {entries.length && !visible.length ? (
                  <li className="open-folder-empty">Nothing matches “{filter}”.</li>
                ) : null}
              </ul>
            ) : (
              <NothingHere path={view?.path} containerized={view?.containerized} />
            )}
          </>
        )}

        {error ? <div className="open-folder-error">{error}</div> : null}

        <div className="open-folder-actions">
          {view?.attach_blocked_reason && !error ? (
            <p className="open-folder-note">{view.attach_blocked_reason}</p>
          ) : null}
          <button
            className="ws-primary"
            type="button"
            onClick={attach}
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
 * A directory with nothing in it. Inside a container that is usually a mount
 * that was never pointed anywhere, which is a settings problem rather than a
 * navigation one -- so this names the setting instead of leaving a blank panel.
 */
function NothingHere({ path, containerized }) {
  if (!containerized) {
    return <p className="open-folder-empty">This folder has no subfolders.</p>;
  }
  return (
    <div className="open-folder-empty-state">
      <p>
        {path ? (
          <>
            <code>{path}</code> is mounted, but there is nothing inside it.
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
