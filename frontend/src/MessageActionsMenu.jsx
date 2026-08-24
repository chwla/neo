import { useEffect, useLayoutEffect, useRef, useState } from "react";

/**
 * The turn's actions, behind a "..." on the bubble's footer.
 *
 * Same popover mechanics as the composer's "+": escape and an outside click
 * close it, and choosing an action closes it too.
 */
//: Roughly the tallest this menu gets (six items plus padding, the agent-turn
//: case). Measuring the menu itself would need it laid out first, which is the
//: thing being decided.
const MENU_SPACE = 210;

export function MessageActionsMenu({ label, children }) {
  const [open, setOpen] = useState(false);
  const [dropUp, setDropUp] = useState(false);
  const menuRef = useRef(null);
  const buttonRef = useRef(null);

  // The last message in a transcript sits against the composer, which floats
  // above it. Opening downward there puts the items under the composer, where
  // the clicks land on the composer instead. So the menu flips up when the room
  // below is not enough -- decided before paint, so it never opens in the wrong
  // place and jumps.
  useLayoutEffect(() => {
    if (!open) {
      return;
    }
    const rect = buttonRef.current?.getBoundingClientRect();
    setDropUp(Boolean(rect) && window.innerHeight - rect.bottom < MENU_SPACE);
  }, [open]);

  useEffect(() => {
    if (!open) {
      return undefined;
    }

    function onPointerDown(event) {
      if (menuRef.current?.contains(event.target) || buttonRef.current?.contains(event.target)) {
        return;
      }
      setOpen(false);
    }

    function onKeyDown(event) {
      if (event.key === "Escape") {
        setOpen(false);
        buttonRef.current?.focus();
      }
    }

    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <span className="message-actions">
      <button
        ref={buttonRef}
        type="button"
        className={`message-actions-trigger${open ? " is-open" : ""}`}
        aria-expanded={open}
        aria-haspopup="true"
        aria-label={label}
        title={label}
        onClick={() => setOpen((current) => !current)}
      >
        {"\u22ef"}
      </button>
      <span
        ref={menuRef}
        className={`message-actions-menu${dropUp ? " drop-up" : ""}`}
        hidden={!open}
        onClick={() => setOpen(false)}
      >
        {children}
      </span>
    </span>
  );
}
