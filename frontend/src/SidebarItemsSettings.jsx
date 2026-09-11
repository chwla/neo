/**
 * Which SYSTEM entries stay pinned in the sidebar.
 *
 * A toggle per entry, saved to the profile database the moment it is flipped --
 * no Save button, because there is nothing here to get half-finished and
 * nothing that needs confirming. It paints first, so the sidebar changes under
 * the dialog while you are looking at it.
 *
 * **Nothing here is ever disabled while a write is in flight.** Seven toggles
 * that lock each other out are seven toggles you can only use one at a time,
 * with the round trip sitting exactly where the second click wants to go. The
 * clicks are never refused; the writes behind them cope instead -- see
 * `setWriter.js`, which coalesces them into one in-flight request that always
 * carries the newest state.
 */

import { useEffect, useRef, useState } from "react";

import { Modal } from "./App.jsx";
import { api } from "./api.js";
import { createSetWriter } from "./setWriter.js";
import { SYSTEM_NAV } from "./systemNav.js";

export default function SidebarItemsSettings({ hidden, onHiddenChange, onClose, backLabel, onBack }) {
  const [error, setError] = useState("");

  /* The writer outlives every render and the callbacks do not, so it is built
     once against this box and reads whatever is in it at the time. Assigning on
     each render rather than in an effect, because a reply can land before
     effects for that render have run. */
  const handlers = useRef({ onHiddenChange, setError });
  handlers.current = { onHiddenChange, setError };

  const writerRef = useRef(null);
  if (!writerRef.current) {
    writerRef.current = createSetWriter({
      send: async (next) => (await api.updateSidebarNavConfig({ hidden: next })).hidden,
      onState: (value) => handlers.current.onHiddenChange(value),
      onError: () =>
        handlers.current.setError("Could not save that. The sidebar is back to what was last saved."),
      initial: hidden,
    });
  }
  const writer = writerRef.current;

  /* The panel can be open before the first load lands, and that reply arrives as
     a prop rather than through the writer. After an optimistic paint the prop is
     already the writer's own value, so this is a no-op for its own updates. */
  useEffect(() => { writer.adopt(hidden); }, [writer, hidden]);

  function toggle(id, pinned) {
    const from = writer.desired();
    writer.set(pinned ? from.filter((item) => item !== id) : [...from, id]);
    setError("");
  }

  const off = new Set(hidden);
  const remaining = SYSTEM_NAV.length - off.size;

  return (
    <Modal title="Sidebar items" onClose={onClose} backLabel={backLabel} onBack={onBack}>
      <p className="dialog-caption">
        Everything Neo can take you to from the sidebar's SYSTEM section. Turning one off
        hides it from the list — it does not delete anything, and the screen stays reachable
        from Settings and from search.
      </p>
      {error ? <div className="appearance-error" role="alert">{error}</div> : null}
      <div className="chat-tools-list">
        {SYSTEM_NAV.map((item) => {
          const pinned = !off.has(item.id);
          return (
            <div className="chat-tools-row" key={item.id}>
              <div className="chat-tools-row-info">
                <div className="chat-tools-row-title">
                  <strong>{item.label}</strong>
                </div>
                <p>{item.description}</p>
              </div>
              <label className="chat-tools-toggle">
                <input
                  type="checkbox"
                  checked={pinned}
                  onChange={(event) => toggle(item.id, event.target.checked)}
                  /* The row's <strong> is not the control's label -- it sits in a
                     sibling -- so without this the checkbox is announced as an
                     unnamed one, seven times over. */
                  aria-label={`Pin ${item.label} to the sidebar`}
                />
              </label>
            </div>
          );
        })}
      </div>
      <p className="dialog-caption">
        {remaining === 0
          ? "The SYSTEM section is hidden while nothing is pinned."
          : `${remaining} of ${SYSTEM_NAV.length} pinned.`}
      </p>
    </Modal>
  );
}
