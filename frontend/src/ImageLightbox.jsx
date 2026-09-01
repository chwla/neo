import { useCallback, useEffect, useRef } from "react";
import { createPortal } from "react-dom";

import { api } from "./api.js";
import { registerModal } from "./modalStack.js";
import Icon from "./WorkspaceIcon.jsx";

/**
 * One image, as large as the window allows.
 *
 * Portalled to <body> rather than drawn in place. Every surface that shows a
 * thumbnail -- the composer, a turn in the transcript, the gallery grid -- sits
 * inside something with its own stacking and overflow, so an overlay rendered
 * where the thumbnail lives would be clipped by the bubble or the composer card
 * instead of covering the screen.
 *
 * Escape goes through the shared modal stack so it closes this and not whatever
 * dialog happens to be underneath it.
 */
export default function ImageLightbox({ images = [], index = 0, onIndexChange, onClose, action = null }) {
  const count = images.length;
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => registerModal(() => closeRef.current?.()), []);

  const step = useCallback(
    (delta) => {
      if (!onIndexChange || count < 2) return;
      onIndexChange((index + delta + count) % count);
    },
    [count, index, onIndexChange],
  );

  useEffect(() => {
    function onKeyDown(event) {
      if (event.key === "ArrowRight") { event.preventDefault(); step(1); }
      else if (event.key === "ArrowLeft") { event.preventDefault(); step(-1); }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [step]);

  const current = images[index];
  if (!current || typeof document === "undefined") return null;

  const label = current.title || "";
  const source = current.src || api.galleryImageUrl(current.id);
  const stepping = count > 1 && Boolean(onIndexChange);

  return createPortal(
    <div className="lbx" role="dialog" aria-modal="true" aria-label={label || "Image"}>
      {/* Clicking away closes, which is what everyone tries first. */}
      <button className="lbx-scrim" type="button" onClick={onClose} aria-label="Close image" />

      {stepping && (
        <button className="lbx-step left" type="button" onClick={() => step(-1)} aria-label="Previous image">
          <Icon name="back" />
        </button>
      )}

      <figure className="lbx-figure">
        <img src={source} alt={current.alt || label || "Image"} />
        {(label || action) && (
          <figcaption>
            {label && <strong>{label}</strong>}
            {current.meta && <span>{current.meta}</span>}
            {action && (
              <button type="button" className="lbx-action" onClick={action.onClick}>
                {action.label}
                <Icon name="next" />
              </button>
            )}
          </figcaption>
        )}
      </figure>

      {stepping && (
        <button className="lbx-step right" type="button" onClick={() => step(1)} aria-label="Next image">
          <Icon name="next" />
        </button>
      )}

      <button className="lbx-close" type="button" onClick={onClose} aria-label="Close image">
        <Icon name="close" />
      </button>

      {count > 1 && <p className="lbx-count">{index + 1} / {count}</p>}
    </div>,
    document.body,
  );
}
