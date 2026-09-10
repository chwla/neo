/**
 * The chat background picker.
 *
 * Same shape as the theme picker next door, and for the same reason: the dialog
 * is narrow so the transcript stays visible behind it, because motion is judged
 * by watching it rather than by reading its name. Choosing applies before the
 * write lands, and a failed write rolls the screen back rather than leaving it
 * disagreeing with the database.
 *
 * Each control sends only the field it changed. The endpoint treats a missing
 * field as "leave it alone", so choosing an intensity cannot write back a stale
 * background, and the two controls on this screen cannot undo each other.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api.js";
import {
  BACKGROUNDS,
  DEFAULT_BACKGROUND_ID,
  INTENSITIES,
  applyBackground,
} from "./backgrounds/index.js";

function BackgroundCard({ background, selected, onChoose }) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      className={`theme-card ${selected ? "is-selected" : ""}`.trim()}
      onClick={() => onChoose(background.id)}
    >
      <span className="theme-card-text">
        <strong>{background.name}</strong>
        <small>{background.description}</small>
      </span>
      <span className="theme-card-check" aria-hidden="true">
        {selected ? "✓" : ""}
      </span>
    </button>
  );
}

export default function BackgroundSettings({
  background,
  intensity,
  onBackgroundChange,
  onIntensityChange,
  onClose,
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const aliveRef = useRef(true);
  //: Which write is the current one. Browsing the list is the whole point of
  //: this screen, so several writes are routinely in flight at once and their
  //: replies are not guaranteed to return in order -- a slower reply for an
  //: earlier click would otherwise land last and put back a background the user
  //: has already moved on from. Shared by both controls, since both write here.
  const writeRef = useRef(0);

  useEffect(() => () => { aliveRef.current = false; }, []);

  const save = useCallback(async (patch) => {
    const previous = { background, intensity };
    const next = { ...previous, ...patch };
    if (next.background === previous.background && next.intensity === previous.intensity) return;
    const write = (writeRef.current += 1);
    const current = () => aliveRef.current && write === writeRef.current;

    // Paint first: the point of the picker is seeing the change, and waiting
    // for a round trip before showing it makes the choice feel like it did not
    // take.
    applyBackground(next.background);
    onBackgroundChange(next.background);
    onIntensityChange(next.intensity);
    setError("");
    setSaving(true);
    try {
      const config = await api.updateAppearanceConfig(patch);
      if (!current()) return;
      // The server answers with the whole configuration, so take its word for
      // what is stored rather than assuming the write landed as sent.
      applyBackground(config.background);
      onBackgroundChange(config.background);
      onIntensityChange(config.intensity);
    } catch {
      if (!current()) return;
      applyBackground(previous.background);
      onBackgroundChange(previous.background);
      onIntensityChange(previous.intensity);
      const name =
        BACKGROUNDS.find((entry) => entry.id === previous.background)?.name || previous.background;
      setError(`Could not save that background. Still using ${name}.`);
    } finally {
      if (current()) setSaving(false);
    }
  }, [background, intensity, onBackgroundChange, onIntensityChange]);

  return (
    <div className="appearance-settings">
      {/* No heading: the dialog frame supplies it, and a second one would be
          announced twice by a screen reader. */}
      <p className="appearance-lede">
        Motion behind the conversation, drawn in the theme&rsquo;s own accent. It sits
        under the messages and never takes a click, and it holds still if your
        system asks for reduced motion.
      </p>

      <div className="theme-grid" role="radiogroup" aria-label="Background">
        {BACKGROUNDS.map((entry) => (
          <BackgroundCard
            key={entry.id}
            background={entry}
            selected={entry.id === background}
            onChoose={(id) => save({ background: id })}
          />
        ))}
      </div>

      <div className="bg-intensity" role="radiogroup" aria-label="Background intensity">
        <span className="bg-intensity-label">Intensity</span>
        {INTENSITIES.map((entry) => (
          <button
            key={entry.id}
            type="button"
            role="radio"
            aria-checked={entry.id === intensity}
            className={`bg-intensity-option ${entry.id === intensity ? "is-selected" : ""}`.trim()}
            onClick={() => save({ intensity: entry.id })}
          >
            {entry.name}
          </button>
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

export { DEFAULT_BACKGROUND_ID };
