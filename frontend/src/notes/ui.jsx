import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { registerModal } from "../modalStack.js";

/**
 * A menu: a dropdown under a button, or a context menu at a point (right-click,
 * long-press). Escape closes the topmost one only (modal stack), an outside
 * press closes it, arrow keys move between items and Enter chooses. Items:
 * { label, onSelect, danger?, disabled?, checked?, items? (a submenu) } or
 * { separator: true }.
 */
/**
 * A dialog's backdrop over what can actually be seen: with an on-screen
 * keyboard up, the browser shows only part of the page (the visual viewport),
 * so the backdrop follows that part and the dialog, buttons included, stays
 * in sight above the keyboard. Only while the dialog is open, on its own
 * element: typing in a note never restyles anything for it.
 */
export function useVisibleArea(ref) {
  useLayoutEffect(() => {
    const viewport = window.visualViewport;
    const element = ref.current;
    if (!viewport || !element) return undefined;
    const update = () => {
      const keyboard = window.innerHeight - viewport.height - viewport.offsetTop > 80;
      element.style.top = keyboard ? `${viewport.offsetTop}px` : "";
      element.style.height = keyboard ? `${viewport.height}px` : "";
      element.style.bottom = keyboard ? "auto" : "";
      element.dataset.keyboard = keyboard ? "open" : "closed";
    };
    update();
    viewport.addEventListener("resize", update);
    viewport.addEventListener("scroll", update);
    return () => {
      viewport.removeEventListener("resize", update);
      viewport.removeEventListener("scroll", update);
    };
  }, [ref]);
}

export function Menu({ items, at, onClose, label = "Menu", nested = false }) {
  const ref = useRef(null);
  const [position, setPosition] = useState({ left: at.x, top: at.y, ready: false });
  const [submenu, setSubmenu] = useState(null);

  useEffect(() => {
    const unregister = registerModal(onClose);
    const onPointer = (event) => {
      if (ref.current && !ref.current.contains(event.target)) onClose();
    };
    document.addEventListener("pointerdown", onPointer, true);
    return () => {
      unregister();
      document.removeEventListener("pointerdown", onPointer, true);
    };
  }, [onClose]);

  // Keep the menu on screen, flipping above or to the left of the point when needed.
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const rect = element.getBoundingClientRect();
    const margin = 8;
    const left = Math.max(margin, Math.min(at.x, window.innerWidth - rect.width - margin));
    const top = at.y + rect.height + margin > window.innerHeight ? Math.max(margin, at.y - rect.height) : at.y;
    setPosition({ left, top, ready: true });
    element.querySelector(':scope > [role="menuitem"]:not([disabled])')?.focus();
  }, [at.x, at.y]);

  function onKeyDown(event) {
    // A submenu sits inside its parent's element; each moves through its own items only.
    if (event.target.closest(".nw-menu") !== ref.current) return;
    const entries = [...ref.current.querySelectorAll(':scope > [role="menuitem"]:not([disabled])')];
    const index = entries.indexOf(document.activeElement);
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const step = event.key === "ArrowDown" ? 1 : -1;
      entries[(index + step + entries.length) % entries.length]?.focus();
    } else if (event.key === "Tab") {
      event.preventDefault();
    }
  }

  const menu = (
    <div
      ref={ref}
      className="nw-menu"
      role="menu"
      aria-label={label}
      style={{ left: position.left, top: position.top, visibility: position.ready ? "visible" : "hidden" }}
      onKeyDown={onKeyDown}
      onContextMenu={(event) => event.preventDefault()}
    >
      {items.filter(Boolean).map((item, index) =>
        item.separator ? (
          <div key={`sep-${index}`} className="nw-menu-sep" role="separator" />
        ) : (
          <button
            key={item.label}
            type="button"
            role="menuitem"
            className={`${item.danger ? "danger" : ""} ${item.items ? "has-sub" : ""}`.trim()}
            disabled={item.disabled}
            aria-haspopup={item.items ? "menu" : undefined}
            aria-checked={item.checked === undefined ? undefined : item.checked}
            onClick={(event) => {
              if (item.items) {
                const rect = event.currentTarget.getBoundingClientRect();
                setSubmenu({ items: item.items, at: { x: rect.right - 4, y: rect.top }, label: item.label });
                return;
              }
              onClose();
              item.onSelect?.();
            }}
          >
            <span className="nw-menu-check" aria-hidden="true">{item.checked ? "✓" : ""}</span>
            <span>{item.label}</span>
          </button>
        ),
      )}
      {submenu && (
        // Inside this menu's element (it is fixed, so nothing clips it): a press
        // on it is then not a press outside this menu, which would close both.
        <Menu
          nested
          items={submenu.items.map((entry) => (entry.separator ? entry : { ...entry, onSelect: () => { onClose(); entry.onSelect?.(); } }))}
          at={submenu.at}
          label={submenu.label}
          onClose={() => setSubmenu(null)}
        />
      )}
    </div>
  );
  return nested ? menu : createPortal(menu, document.body);
}

/** A long press (touch) or right-click opens a context menu. Returns event props. */
export function useContextMenu(open) {
  const timer = useRef(null);
  const origin = useRef(null);
  const fired = useRef(false);
  const cancel = () => {
    clearTimeout(timer.current);
    timer.current = null;
  };
  return {
    onContextMenu(event) {
      event.preventDefault();
      open({ x: event.clientX, y: event.clientY });
    },
    onPointerDown(event) {
      fired.current = false;
      if (event.pointerType !== "touch") return;
      origin.current = { x: event.clientX, y: event.clientY };
      cancel();
      timer.current = setTimeout(() => {
        fired.current = true;
        open(origin.current);
      }, 500);
    },
    onPointerMove(event) {
      if (!timer.current || !origin.current) return;
      if (Math.hypot(event.clientX - origin.current.x, event.clientY - origin.current.y) > 10) cancel();
    },
    onPointerUp: cancel,
    onPointerCancel: cancel,
    /** True when the press that just ended opened the menu (so it is not also a click). */
    consumedPress() {
      const value = fired.current;
      fired.current = false;
      return value;
    },
  };
}

export function ConfirmDialog({ title, message, confirmLabel, danger = false, onConfirm, onCancel }) {
  const confirmRef = useRef(null);
  const scrim = useRef(null);
  useVisibleArea(scrim);
  useEffect(() => {
    const unregister = registerModal(onCancel);
    confirmRef.current?.focus();
    return unregister;
  }, [onCancel]);
  return createPortal(
    <div ref={scrim} className="nw-scrim" onPointerDown={(event) => event.target === event.currentTarget && onCancel()}>
      <div className="nw-dialog" role="alertdialog" aria-modal="true" aria-labelledby="nw-dialog-title" aria-describedby="nw-dialog-message">
        <h2 id="nw-dialog-title">{title}</h2>
        <p id="nw-dialog-message">{message}</p>
        <div className="nw-dialog-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button ref={confirmRef} type="button" className={danger ? "danger" : "primary"} onClick={onConfirm}>
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

export function PromptDialog({ title, label, initial = "", confirmLabel, onSubmit, onCancel }) {
  const [value, setValue] = useState(initial);
  const input = useRef(null);
  const scrim = useRef(null);
  useVisibleArea(scrim);
  useEffect(() => {
    const unregister = registerModal(onCancel);
    input.current?.select();
    return unregister;
  }, [onCancel]);
  return createPortal(
    <div ref={scrim} className="nw-scrim" onPointerDown={(event) => event.target === event.currentTarget && onCancel()}>
      <form
        className="nw-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="nw-prompt-title"
        onSubmit={(event) => {
          event.preventDefault();
          if (value.trim()) onSubmit(value.trim());
        }}
      >
        <h2 id="nw-prompt-title">{title}</h2>
        <label className="nw-dialog-field">
          <span>{label}</span>
          <input ref={input} value={value} onChange={(event) => setValue(event.target.value)} maxLength={100} />
        </label>
        <div className="nw-dialog-actions">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button type="submit" className="primary" disabled={!value.trim()}>{confirmLabel}</button>
        </div>
      </form>
    </div>,
    document.body,
  );
}

export function Toast({ toast, onDismiss }) {
  if (!toast) return null;
  return (
    <div className="nw-toast" role="status" aria-live="polite">
      <span>{toast.text}</span>
      {toast.action && (
        <button
          type="button"
          onClick={() => {
            onDismiss();
            toast.action();
          }}
        >
          {toast.actionLabel}
        </button>
      )}
    </div>
  );
}
