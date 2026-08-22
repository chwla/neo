import { useEffect, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";

/**
 * Pointing Neo at a folder it will edit directly.
 *
 * Typed rather than picked, because a browser folder picker deliberately does
 * not reveal an absolute path -- it yields names relative to the folder chosen,
 * which is exactly what the server cannot use. The recent list is what keeps
 * that from being a tax on every run: the first attach is typed, the rest are a
 * click.
 */
export default function OpenFolderDialog({ projectId, onClose, onAttached }) {
  const [path, setPath] = useState("");
  const [managed, setManaged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [roots, setRoots] = useState({ roots: [], recent: [], containerized: false });

  useEffect(() => {
    api.repoRoots().then(setRoots).catch(() => {});
  }, []);

  async function attach(chosen) {
    const target = (chosen ?? path).trim();
    if (!target) return;
    setBusy(true);
    setError("");
    try {
      const result = await api.attachFolder({ path: target, projectId, managed });
      onAttached(result.repo, result.stats);
      onClose();
    } catch (attachError) {
      setError(attachError.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Open a folder" onClose={onClose} className="open-folder-dialog">
      <form
        className="ws-register"
        onSubmit={(event) => {
          event.preventDefault();
          attach();
        }}
      >
        <label className="ws-field">
          <span>Folder path</span>
          <input
            value={path}
            autoFocus
            onChange={(event) => setPath(event.target.value)}
            placeholder={roots.roots?.[0] ? `${roots.roots[0]}/my-project` : "/absolute/path/to/project"}
            aria-label="Absolute path to the folder"
          />
        </label>

        {roots.containerized ? (
          <p className="ws-help">
            Neo is running in a container, so it can only reach folders mounted into
            it{roots.roots?.length ? <> — under <code>{roots.roots.join(", ")}</code></> : null}.
            Use the path as it appears inside the container, not the one on your
            computer: <code>/workspace/my-project</code>, not <code>C:\Users\you\code</code>.
          </p>
        ) : null}

        {roots.recent?.length ? (
          <div className="ws-field">
            <span>Recent</span>
            <div className="agent-folder-recents">
              {roots.recent.map((item) => (
                <button
                  key={item.repo_id}
                  type="button"
                  className="neo-button secondary"
                  disabled={busy}
                  title={item.path}
                  onClick={() => attach(item.path)}
                >
                  {item.name}
                </button>
              ))}
            </div>
          </div>
        ) : null}

        <label className="ws-consent">
          <input
            type="checkbox"
            checked={managed}
            onChange={(event) => setManaged(event.target.checked)}
          />
          <span>
            Work on a copy instead. Your files are left untouched and you apply the
            changes yourself afterwards.
          </span>
        </label>

        <p className="ws-help">
          {managed
            ? "Neo copies the supported text files and the agent edits the copy."
            : "The agent reads and edits these files directly, like a coding CLI. Every run is journalled, so you can undo it."}
        </p>

        {error ? <div className="ws-notice">{error}</div> : null}

        <button className="ws-primary" type="submit" disabled={busy || !path.trim()}>
          {busy ? "Opening…" : managed ? "Copy folder" : "Open folder"}
        </button>
      </form>
    </Modal>
  );
}
