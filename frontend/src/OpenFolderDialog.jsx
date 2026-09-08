import { useCallback, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";
import FolderBrowser from "./FolderBrowser.jsx";

/**
 * Choosing the folder Agent Mode will edit, by browsing the real filesystem.
 *
 * The browsing half lives in ``FolderBrowser``, shared with the Skills panel.
 * What stays here is the part that is only true of a workspace: whether the
 * current folder may be attached at all, and the sentence explaining why not
 * when the server says it may not.
 */
export default function OpenFolderDialog({ projectId, onClose, onAttached }) {
  const [view, setView] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  /** Open `path` as the workspace. Always a container path, never a display one. */
  const select = useCallback(
    async (path) => {
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
    },
    [projectId, onAttached, onClose],
  );

  return (
    <Modal title="Open a folder" onClose={onClose} className="open-folder">
      <div className="open-folder-body">
        <FolderBrowser
          busy={busy}
          onSelect={select}
          selectLabel="Select"
          selectTitle={(entry) =>
            `Open ${entry.display_path || entry.path} as the workspace`
          }
          onViewChange={setView}
        />

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
            disabled={busy || !view?.can_attach}
          >
            {busy ? "Opening…" : "Open this folder"}
          </button>
        </div>
      </div>
    </Modal>
  );
}
