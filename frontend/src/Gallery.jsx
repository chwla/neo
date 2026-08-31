import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api.js";
import Icon from "./WorkspaceIcon.jsx";

const STATUS_LABEL = {
  pending: "Describing…",
  ready: "Described",
  failed: "Not described",
  skipped: "Skipped",
};

function shortDate(iso) {
  if (!iso) return "";
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return "";
  return value.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function dimensions(item) {
  if (!item.width || !item.height) return "";
  return `${item.width} × ${item.height}`;
}

/**
 * Everything Neo has seen, searchable by what was in it.
 *
 * The search box is not a filter over filenames -- it posts to the ranker, which
 * fuses the transcribed text, the description, when the image was seen and which
 * conversation it appeared in. So "last week" and "approval button" in one line
 * do what the user means, and the window it resolved is shown back to them.
 */
export default function Gallery({ onBack, onOpenChat, initialItemId = null }) {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [selected, setSelected] = useState(null);
  const [appearances, setAppearances] = useState([]);
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [window_, setWindow] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState(null);
  const input = useRef(null);

  const load = useCallback(async () => {
    const term = query.trim();
    if (!term) {
      const data = await api.galleryList({ status: status || undefined });
      setItems(data.items || []);
      setTotal(data.total || 0);
      setWindow(null);
      return;
    }
    const data = await api.searchGallery({ query: term, limit: 60 });
    const hits = (data.results || []).map((hit) => hit.item);
    setItems(status ? hits.filter((item) => item.description_status === status) : hits);
    setTotal(hits.length);
    setWindow(data.window || null);
  }, [query, status]);

  const open = useCallback(async (itemId) => {
    const data = await api.galleryItem(itemId);
    setSelected(data.item);
    setAppearances(data.appearances || []);
    setDraft(null);
    setError("");
  }, []);

  useEffect(() => {
    load().catch((err) => setError(err.message));
  }, [load]);

  useEffect(() => {
    if (initialItemId) open(initialItemId).catch((err) => setError(err.message));
  }, [initialItemId, open]);

  async function upload(event) {
    const files = Array.from(event.target.files || []);
    if (!files.length) return;
    setBusy(true);
    setError("");
    try {
      for (const file of files) await api.uploadGalleryImage(file, { origin: "upload" });
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
      if (input.current) input.current.value = "";
    }
  }

  async function save() {
    if (!draft || !selected) return;
    setBusy(true);
    try {
      const data = await api.updateGalleryItem(selected.id, {
        title: draft.title,
        caption: draft.caption,
        alt_text: draft.alt_text,
        tags: draft.tags
          .split(",")
          .map((tag) => tag.trim().toLowerCase())
          .filter(Boolean),
      });
      setSelected(data.item);
      setDraft(null);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function remove(purge) {
    if (!selected) return;
    setBusy(true);
    try {
      await api.deleteGalleryItem(selected.id, { purge });
      setSelected(null);
      setAppearances([]);
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function redescribe() {
    if (!selected) return;
    setBusy(true);
    try {
      await api.describeGalleryItem(selected.id);
      await open(selected.id);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  function startEditing() {
    setDraft({
      title: selected.title || "",
      caption: selected.caption || "",
      alt_text: selected.alt_text || "",
      tags: (selected.tags || []).join(", "),
    });
  }

  return (
    <div className="ws">
      <aside className="ws-rail">
        <header className="ws-rail-head">
          <div className="ws-rail-top">
            <button className="ws-back" type="button" onClick={onBack}>
              <Icon name="back" />
              Chat
            </button>
            <span className="ws-rail-count">
              {total} image{total === 1 ? "" : "s"}
            </span>
          </div>
          <h1 className="ws-rail-title">Gallery</h1>
          <input ref={input} type="file" accept="image/*" multiple hidden onChange={upload} />
          <button
            className="ws-primary"
            type="button"
            disabled={busy}
            onClick={() => input.current?.click()}
          >
            <Icon name="upload" />
            Add images
          </button>
        </header>

        <div className="ws-filters">
          <div className="ws-search">
            <Icon name="search" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="What was in it? e.g. approval button last week"
              aria-label="Search the gallery"
            />
            {query && (
              <button
                className="ws-search-clear"
                type="button"
                onClick={() => setQuery("")}
                aria-label="Clear search"
              >
                ×
              </button>
            )}
          </div>
          {window_ && (
            <p className="ws-empty-line">
              Narrowed to “{window_.phrase}”: {shortDate(window_.start)} – {shortDate(window_.end)}
            </p>
          )}
          <div className="ws-filter-row">
            <select
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              aria-label="Filter by description status"
            >
              <option value="">Any description state</option>
              <option value="ready">Described</option>
              <option value="pending">Describing</option>
              <option value="failed">Not described</option>
            </select>
          </div>
        </div>

        <div className="ws-list gallery-grid">
          {items.length === 0 ? (
            <p className="ws-empty-line">
              {query.trim()
                ? "No image matched that."
                : "Nothing here yet. Images you paste into a chat land here automatically."}
            </p>
          ) : (
            items.map((item) => (
              <button
                key={item.id}
                type="button"
                className={`gallery-tile ${selected?.id === item.id ? "is-active" : ""}`.trim()}
                onClick={() => open(item.id).catch((err) => setError(err.message))}
                title={item.title || "Untitled"}
              >
                <img
                  src={api.galleryThumbnailUrl(item.id)}
                  alt={item.alt_text || item.title || "Gallery image"}
                  loading="lazy"
                />
                <span className="gallery-tile-name">{item.title || "Untitled"}</span>
                {item.description_status !== "ready" && (
                  <span className="gallery-tile-state">{STATUS_LABEL[item.description_status]}</span>
                )}
              </button>
            ))
          )}
        </div>
      </aside>

      <section className="ws-main">
        {!selected ? (
          <div className="ws-empty">
            <p>Select an image to see what Neo knows about it.</p>
          </div>
        ) : (
          <>
            <div className="ws-doc-head">
              <div>
                <h2 className="ws-doc-title">{selected.title || "Untitled"}</h2>
                <p className="ws-doc-meta">
                  {[dimensions(selected), selected.image_format, STATUS_LABEL[selected.description_status]]
                    .filter(Boolean)
                    .join(" · ")}
                </p>
              </div>
              <div className="ws-doc-actions">
                <button className="ws-action" type="button" disabled={busy} onClick={redescribe}>
                  <Icon name="sparkle" />
                  Describe again
                </button>
                <button className="ws-action" type="button" disabled={busy} onClick={() => remove(false)}>
                  <Icon name="trash" />
                  Delete
                </button>
                <button className="ws-action" type="button" disabled={busy} onClick={() => remove(true)}>
                  Delete permanently
                </button>
              </div>
            </div>

            <div className="ws-stage">
              <div className="ws-doc wide">
                <section className="ws-section">
                  <img
                    className="gallery-full"
                    src={api.galleryImageUrl(selected.id)}
                    alt={selected.alt_text || selected.title || "Gallery image"}
                  />
                </section>

                <section className="ws-section">
                  <div className="ws-section-head">
                    <h3 className="ws-section-title">Description</h3>
                    {!draft && (
                      <button className="ws-action" type="button" onClick={startEditing}>
                        Edit
                      </button>
                    )}
                  </div>
                  {draft ? (
                    <>
                      <label className="ws-field">
                        <span>Title</span>
                        <input
                          value={draft.title}
                          onChange={(event) => setDraft({ ...draft, title: event.target.value })}
                        />
                      </label>
                      <label className="ws-field">
                        <span>Caption</span>
                        <textarea
                          rows={4}
                          value={draft.caption}
                          onChange={(event) => setDraft({ ...draft, caption: event.target.value })}
                        />
                      </label>
                      <label className="ws-field">
                        <span>Alt text</span>
                        <input
                          value={draft.alt_text}
                          onChange={(event) => setDraft({ ...draft, alt_text: event.target.value })}
                        />
                      </label>
                      <label className="ws-field">
                        <span>Tags</span>
                        <input
                          value={draft.tags}
                          onChange={(event) => setDraft({ ...draft, tags: event.target.value })}
                          placeholder="comma, separated"
                        />
                      </label>
                      <div className="ws-doc-actions">
                        <button className="ws-primary" type="button" disabled={busy} onClick={save}>
                          Save
                        </button>
                        <button className="ws-action" type="button" onClick={() => setDraft(null)}>
                          Cancel
                        </button>
                      </div>
                    </>
                  ) : (
                    <>
                      <p className="ws-pre-line">
                        {selected.caption || "No description yet."}
                      </p>
                      {selected.description_error && (
                        <p className="ws-error">{selected.description_error}</p>
                      )}
                      {(selected.tags || []).length > 0 && (
                        <p className="ws-doc-meta">{selected.tags.join(" · ")}</p>
                      )}
                      {selected.user_edited && (
                        <p className="ws-doc-meta">
                          Edited by you — describing again will keep your words.
                        </p>
                      )}
                    </>
                  )}
                </section>

                {selected.ocr_text && (
                  <section className="ws-section">
                    <h3 className="ws-section-title">Text in this image</h3>
                    <pre className="ws-pre">{selected.ocr_text}</pre>
                  </section>
                )}

                <section className="ws-section">
                  <h3 className="ws-section-title">Where this appeared</h3>
                  {appearances.length === 0 ? (
                    <p className="ws-empty-line">Not shown in a conversation yet.</p>
                  ) : (
                    <ul className="ws-link-list">
                      {appearances.map((appearance) => (
                        <li key={appearance.id}>
                          <span>{shortDate(appearance.seen_at)}</span>
                          {appearance.chat_id != null && (
                            <button
                              className="ws-action"
                              type="button"
                              onClick={() => onOpenChat?.(appearance.chat_id)}
                            >
                              Open that conversation
                            </button>
                          )}
                        </li>
                      ))}
                    </ul>
                  )}
                </section>

                <section className="ws-section">
                  <h3 className="ws-section-title">Reference</h3>
                  <p className="ws-doc-meta">
                    Id <code>{selected.id}</code> — ask Neo about it by name, or say what was in it.
                  </p>
                </section>

                {error && <div className="ws-error">{error}</div>}
              </div>
            </div>
          </>
        )}
      </section>
    </div>
  );
}
