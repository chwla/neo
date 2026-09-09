/**
 * The theme picker.
 *
 * Choosing applies immediately, before the write to the server has finished, and
 * the dialog is deliberately narrow so the interface stays visible behind it --
 * a palette is judged by looking at it, not by reading its name, so the preview
 * is the app itself rather than a swatch. The write is optimistic with a
 * rollback, the same shape as KeyboardSettings: on failure the previous theme
 * goes back on the document and the error says which one it went back to, so a
 * failed save never leaves the screen and the database disagreeing.
 *
 * The cards show their palette as five chips rather than a screenshot because a
 * screenshot goes stale the moment any surface changes, and these come from the
 * same list the stylesheet is generated against.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api.js";
import { DEFAULT_THEME_ID, THEMES, applyTheme } from "./themes.js";

/** One theme's card: its name, what it is for, and its palette. */
function ThemeCard({ theme, selected, onChoose }) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      className={`theme-card ${selected ? "is-selected" : ""}`.trim()}
      onClick={() => onChoose(theme.id)}
    >
      <span className="theme-card-swatch" aria-hidden="true">
        {theme.swatch.map((color, i) => (
          <span key={i} style={{ background: color }} />
        ))}
      </span>
      <span className="theme-card-text">
        <strong>{theme.name}</strong>
        <small>{theme.description}</small>
      </span>
      <span className="theme-card-check" aria-hidden="true">
        {selected ? "✓" : ""}
      </span>
    </button>
  );
}

export default function AppearanceSettings({ theme, onThemeChange, onClose }) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const aliveRef = useRef(true);
  //: Which write is the current one. Browsing the list is the whole point of
  //: this screen, so several writes are routinely in flight at once, and their
  //: replies are not guaranteed to come back in the order they were sent -- a
  //: slower reply for an earlier click would otherwise land last and repaint
  //: the interface in a theme the user has already moved on from, leaving the
  //: screen and the database disagreeing until the next reload.
  const writeRef = useRef(0);

  useEffect(() => () => { aliveRef.current = false; }, []);

  const choose = useCallback(async (next) => {
    if (next === theme) return;
    const previous = theme;
    const write = (writeRef.current += 1);
    const current = () => aliveRef.current && write === writeRef.current;
    // Paint first: the point of the picker is seeing the change, and waiting for
    // a round trip to the local server before showing it makes the choice feel
    // like it did not take.
    applyTheme(next);
    onThemeChange(next);
    setError("");
    setSaving(true);
    try {
      const config = await api.updateAppearanceConfig({ theme: next });
      if (!current()) return;
      // The server answers with the whole configuration, so take its word for
      // what is stored rather than assuming the write landed as sent.
      applyTheme(config.theme);
      onThemeChange(config.theme);
    } catch (caught) {
      if (!current()) return;
      applyTheme(previous);
      onThemeChange(previous);
      const name = THEMES.find((t) => t.id === previous)?.name || previous;
      setError(`Could not save that theme. Still using ${name}.`);
    } finally {
      if (current()) setSaving(false);
    }
  }, [theme, onThemeChange]);

  return (
    <div className="appearance-settings">
      {/* No heading: the dialog frame supplies it, and a second one would be
          announced twice by a screen reader. */}
      <p className="appearance-lede">
        Every colour in Neo comes from the theme, so this changes the whole
        interface rather than just the accent.
      </p>

      <div className="theme-grid" role="radiogroup" aria-label="Theme">
        {THEMES.map((entry) => (
          <ThemeCard
            key={entry.id}
            theme={entry}
            selected={entry.id === theme}
            onChoose={choose}
          />
        ))}
      </div>

      {error ? (
        <div className="appearance-error" role="alert">{error}</div>
      ) : null}

      <footer className="appearance-foot">
        <span className="appearance-saved" role="status">
          {saving ? "Saving…" : ""}
        </span>
        <button type="button" className="neo-button" onClick={onClose}>
          Done
        </button>
      </footer>
    </div>
  );
}

export { DEFAULT_THEME_ID };
