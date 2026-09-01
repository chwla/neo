import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api.js";
import Icon from "./WorkspaceIcon.jsx";

const STATUS_LABEL = {
  pending: "Describing",
  ready: "Described",
  failed: "Not described",
  skipped: "Skipped",
};

const ORIGIN_LABEL = {
  chat_attachment: "From a chat",
  paste: "Pasted",
  upload: "Uploaded",
  generated: "Generated",
};

const SORTS = {
  recent: { label: "Newest first", compare: (a, b) => key(b.created_at) - key(a.created_at) },
  oldest: { label: "Oldest first", compare: (a, b) => key(a.created_at) - key(b.created_at) },
  name: {
    label: "By name",
    compare: (a, b) => (a.title || "Untitled").localeCompare(b.title || "Untitled"),
  },
};

function key(iso) {
  const value = Date.parse(iso || "");
  return Number.isNaN(value) ? 0 : value;
}

function shortDate(iso) {
  if (!iso) return "";
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return "";
  return value.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}

function relativeDate(iso) {
  const time = key(iso);
  if (!time) return "";
  const diff = Date.now() - time;
  const day = 86400000;
  if (diff < 3600000) return `${Math.max(1, Math.floor(diff / 60000))}m ago`;
  if (diff < day) return `${Math.floor(diff / 3600000)}h ago`;
  if (diff < 7 * day) return `${Math.floor(diff / day)}d ago`;
  return shortDate(iso);
}

function dimensions(item) {
  if (!item?.width || !item?.height) return "";
  return `${item.width} × ${item.height}`;
}

/**
 * Everything Neo has seen, searchable by what was in it.
 *
 * The images get the whole width. A gallery whose pictures live in a 320px
 * sidebar is a list with thumbnails, not a gallery -- so the grid is the page,
 * what Neo knows about one image is a panel beside it, and the image itself
 * opens full-bleed with the arrow keys walking the grid underneath.
 *
 * The search box is not a filter over filenames -- it posts to the ranker, which
 * fuses the transcribed text, the description, when the image was seen and which
 * conversation it appeared in. So "last week" and "approval button" in one line
 * do what the user means, and the window it resolved is shown back to them.
 */
export default function Gallery({ onBack, onOpenChat, initialItemId = null }) {
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  /* A request is only ever in flight where effects run, so a static render
     is not loading -- it simply has nothing, and should say so. */
  const [loading, setLoading] = useState(() => typeof window !== "undefined");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const [typed, setTyped] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [origin, setOrigin] = useState("");
  const [tag, setTag] = useState("");
  const [pinnedOnly, setPinnedOnly] = useState(false);
  const [sort, setSort] = useState("recent");
  const [compact, setCompact] = useState(false);
  const [window_, setWindow] = useState(null);

  const [selectedId, setSelectedId] = useState(initialItemId);
  const [detail, setDetail] = useState(null);
  const [appearances, setAppearances] = useState([]);
  const [draft, setDraft] = useState(null);
  const [zoomed, setZoomed] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [showOcr, setShowOcr] = useState(false);
  const [dropping, setDropping] = useState(false);

  const fileInput = useRef(null);
  const searchInput = useRef(null);
  const gridRef = useRef(null);

  /* Every keystroke used to post to the ranker. Let the typing settle first. */
  useEffect(() => {
    const handle = setTimeout(() => setQuery(typed.trim()), 220);
    return () => clearTimeout(handle);
  }, [typed]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      if (!query) {
        const data = await api.galleryList({
          status: status || undefined,
          origin: origin || undefined,
          tags: tag ? [tag] : [],
          pinned: pinnedOnly ? true : undefined,
          limit: 200,
        });
        setItems(data.items || []);
        setTotal(data.total ?? (data.items || []).length);
        setWindow(null);
        return;
      }
      const data = await api.searchGallery({ query, limit: 80, tags: tag ? [tag] : [] });
      let hits = (data.results || []).map((hit) => hit.item);
      if (status) hits = hits.filter((item) => item.description_status === status);
      if (origin) hits = hits.filter((item) => item.origin === origin);
      if (pinnedOnly) hits = hits.filter((item) => item.pinned);
      setItems(hits);
      setTotal(hits.length);
      setWindow(data.window || null);
    } finally {
      setLoading(false);
    }
  }, [origin, pinnedOnly, query, status, tag]);

  useEffect(() => {
    load().catch((err) => setError(err.message));
  }, [load]);

  const open = useCallback(async (itemId) => {
    setSelectedId(itemId);
    setError("");
    setDraft(null);
    setConfirming(false);
    setShowOcr(false);
    try {
      const data = await api.galleryItem(itemId);
      setDetail(data.item);
      setAppearances(data.appearances || []);
    } catch (err) {
      setError(err.message);
    }
  }, []);

  useEffect(() => {
    if (initialItemId) open(initialItemId);
  }, [initialItemId, open]);

  /* Search hits arrive pre-ranked; only a plain listing is ours to order. */
  const ordered = useMemo(
    () => (query ? items : [...items].sort(SORTS[sort].compare)),
    [items, query, sort],
  );

  const tags = useMemo(() => {
    const counts = new Map();
    for (const item of items) {
      for (const name of item.tags || []) counts.set(name, (counts.get(name) || 0) + 1);
    }
    return [...counts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 12);
  }, [items]);

  const index = ordered.findIndex((item) => item.id === selectedId);
  const filtered = Boolean(query || status || origin || tag || pinnedOnly);

  const step = useCallback(
    (delta) => {
      if (!ordered.length) return;
      const from = index < 0 ? (delta > 0 ? -1 : 0) : index;
      const next = Math.min(ordered.length - 1, Math.max(0, from + delta));
      if (ordered[next]) open(ordered[next].id);
    },
    [index, open, ordered],
  );

  /* Arrow keys walk the grid, Enter opens the image, Escape steps back out. */
  useEffect(() => {
    function onKeyDown(event) {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(event.target?.tagName || "");
      if (event.key === "/" && !typing) {
        event.preventDefault();
        searchInput.current?.focus();
        return;
      }
      if (event.key === "Escape") {
        if (zoomed) setZoomed(false);
        else if (typing) event.target.blur();
        else if (selectedId) setSelectedId(null);
        return;
      }
      if (typing) return;
      const columns = gridRef.current?.dataset.columns ? Number(gridRef.current.dataset.columns) : 1;
      if (event.key === "ArrowRight") { event.preventDefault(); step(1); }
      else if (event.key === "ArrowLeft") { event.preventDefault(); step(-1); }
      else if (event.key === "ArrowDown") { event.preventDefault(); step(columns); }
      else if (event.key === "ArrowUp") { event.preventDefault(); step(-columns); }
      else if (event.key === "Enter" && selectedId) { event.preventDefault(); setZoomed(true); }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [selectedId, step, zoomed]);

  /* Track the real column count so ArrowDown lands a row below, not n items on. */
  useEffect(() => {
    const grid = gridRef.current;
    if (!grid || typeof ResizeObserver === "undefined") return undefined;
    const measure = () => {
      const columns = window.getComputedStyle(grid).gridTemplateColumns.split(" ").length;
      grid.dataset.columns = String(Math.max(1, columns));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(grid);
    return () => observer.disconnect();
  }, [ordered.length]);

  async function ingest(files) {
    const images = files.filter((file) => file.type.startsWith("image/"));
    if (!images.length) return;
    setBusy(true);
    setError("");
    try {
      for (const file of images) await api.uploadGalleryImage(file, { origin: "upload" });
      await load();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  }

  async function mutate(work) {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const togglePin = (item) =>
    mutate(async () => {
      const data = await api.updateGalleryItem(item.id, { pinned: !item.pinned });
      if (detail?.id === item.id) setDetail(data.item);
      setItems((current) =>
        current.map((entry) => (entry.id === item.id ? { ...entry, pinned: data.item.pinned } : entry)),
      );
    });

  const save = () =>
    mutate(async () => {
      const data = await api.updateGalleryItem(detail.id, {
        title: draft.title,
        caption: draft.caption,
        alt_text: draft.alt_text,
        tags: draft.tags.split(",").map((entry) => entry.trim().toLowerCase()).filter(Boolean),
      });
      setDetail(data.item);
      setDraft(null);
      await load();
    });

  const remove = (purge) =>
    mutate(async () => {
      await api.deleteGalleryItem(detail.id, { purge });
      setSelectedId(null);
      setDetail(null);
      setAppearances([]);
      setConfirming(false);
      await load();
    });

  const redescribe = () =>
    mutate(async () => {
      await api.describeGalleryItem(detail.id);
      const data = await api.galleryItem(detail.id);
      setDetail(data.item);
      setAppearances(data.appearances || []);
    });

  function clearFilters() {
    setTyped("");
    setQuery("");
    setStatus("");
    setOrigin("");
    setTag("");
    setPinnedOnly(false);
  }

  const selected = selectedId && detail?.id === selectedId ? detail : null;

  return (
    <div className={`gal ${selected ? "has-panel" : ""}`.trim()}>
      <div
        className={`gal-main ${dropping ? "is-dropping" : ""}`.trim()}
        onDragOver={(event) => { event.preventDefault(); setDropping(true); }}
        onDragLeave={(event) => { if (event.currentTarget === event.target) setDropping(false); }}
        onDrop={(event) => {
          event.preventDefault();
          setDropping(false);
          ingest(Array.from(event.dataTransfer.files || []));
        }}
      >
        <header className="gal-head">
          <div className="gal-head-row">
            <button className="gal-back" type="button" onClick={onBack}>
              <Icon name="back" />
              Chat
            </button>
            <div className="gal-title-block">
              <h1 className="gal-title">Gallery</h1>
              <p className="gal-subtitle">
                {loading ? "Loading…" : `${total} image${total === 1 ? "" : "s"}`}
                {filtered && !loading ? " matching" : ""}
              </p>
            </div>
            <input ref={fileInput} type="file" accept="image/*" multiple hidden
              onChange={(event) => ingest(Array.from(event.target.files || []))} />
            <button className="gal-add" type="button" disabled={busy} onClick={() => fileInput.current?.click()}>
              <Icon name="upload" />
              Add images
            </button>
          </div>

          <div className="gal-search">
            <Icon name="search" />
            <input
              ref={searchInput}
              value={typed}
              onChange={(event) => setTyped(event.target.value)}
              placeholder="What was in it? e.g. approval button last week"
              aria-label="Search the gallery"
            />
            {typed ? (
              <button className="gal-search-clear" type="button" onClick={() => setTyped("")} aria-label="Clear search">
                <Icon name="close" />
              </button>
            ) : (
              <kbd className="gal-kbd">/</kbd>
            )}
          </div>

          <div className="gal-controls">
            <div className="gal-control-group">
              <select value={status} onChange={(event) => setStatus(event.target.value)} aria-label="Description state">
                <option value="">Any state</option>
                <option value="ready">Described</option>
                <option value="pending">Describing</option>
                <option value="failed">Not described</option>
              </select>
              <select value={origin} onChange={(event) => setOrigin(event.target.value)} aria-label="Where it came from">
                <option value="">Any source</option>
                {Object.entries(ORIGIN_LABEL).map(([value, label]) => (
                  <option key={value} value={value}>{label}</option>
                ))}
              </select>
              <button
                className={`gal-toggle ${pinnedOnly ? "on" : ""}`.trim()}
                type="button"
                aria-pressed={pinnedOnly}
                onClick={() => setPinnedOnly((value) => !value)}
              >
                <Icon name="pin" />
                Pinned
              </button>
            </div>

            <div className="gal-control-group gal-control-end">
              <select
                value={sort}
                onChange={(event) => setSort(event.target.value)}
                disabled={Boolean(query)}
                title={query ? "Search results stay in relevance order" : "Sort"}
                aria-label="Sort"
              >
                {Object.entries(SORTS).map(([value, entry]) => (
                  <option key={value} value={value}>{entry.label}</option>
                ))}
              </select>
              <div className="gal-density" role="group" aria-label="Grid density">
                <button type="button" className={compact ? "" : "on"} onClick={() => setCompact(false)} aria-label="Comfortable grid">
                  <Icon name="grid" />
                </button>
                <button type="button" className={compact ? "on" : ""} onClick={() => setCompact(true)} aria-label="Dense grid">
                  <Icon name="rows" />
                </button>
              </div>
            </div>
          </div>

          {tags.length > 0 && (
            <div className="gal-tagbar">
              <button
                type="button"
                className={`gal-tagchip ${tag ? "" : "on"}`.trim()}
                onClick={() => setTag("")}
              >
                All
              </button>
              {tags.map(([name, count]) => (
                <button
                  key={name}
                  type="button"
                  className={`gal-tagchip ${tag === name ? "on" : ""}`.trim()}
                  onClick={() => setTag(tag === name ? "" : name)}
                >
                  {name}
                  <em>{count}</em>
                </button>
              ))}
            </div>
          )}

          {window_ && (
            <p className="gal-window">
              <Icon name="clock" />
              Narrowed to “{window_.phrase}” — {shortDate(window_.start)} to {shortDate(window_.end)}
            </p>
          )}

          {error && (
            <div className="gal-error">
              <Icon name="warning" />
              {error}
            </div>
          )}
        </header>

        <div className="gal-scroll">
          {loading && ordered.length === 0 ? (
            <div className={`gal-grid ${compact ? "compact" : ""}`.trim()} aria-hidden="true">
              {Array.from({ length: 12 }, (unused, position) => (
                <div className="gal-skeleton" key={position} />
              ))}
            </div>
          ) : ordered.length === 0 ? (
            <div className="gal-empty">
              <div className="gal-empty-mark"><Icon name="image" /></div>
              {filtered ? (
                <>
                  <h2>Nothing matched</h2>
                  <p>Try fewer words, or drop the filters — search reads what was in the picture, not the filename.</p>
                  <button className="gal-add" type="button" onClick={clearFilters}>Clear filters</button>
                </>
              ) : (
                <>
                  <h2>No images yet</h2>
                  <p>Anything you paste into a chat lands here on its own, described and searchable. You can also drop files straight onto this page.</p>
                  <button className="gal-add" type="button" onClick={() => fileInput.current?.click()}>
                    <Icon name="upload" />
                    Add images
                  </button>
                </>
              )}
            </div>
          ) : (
            <div ref={gridRef} className={`gal-grid ${compact ? "compact" : ""}`.trim()} role="list">
              {ordered.map((item) => (
                <div
                  key={item.id}
                  role="listitem"
                  className={`gal-card ${selectedId === item.id ? "is-active" : ""}`.trim()}
                >
                  <button
                    type="button"
                    className="gal-card-shot"
                    onClick={() => open(item.id)}
                    onDoubleClick={() => { open(item.id); setZoomed(true); }}
                    aria-label={item.title || "Untitled image"}
                  >
                    <img
                      src={api.galleryThumbnailUrl(item.id)}
                      alt={item.alt_text || item.title || "Gallery image"}
                      loading="lazy"
                    />
                    {item.description_status !== "ready" && (
                      <span className={`gal-flag ${item.description_status}`}>
                        {STATUS_LABEL[item.description_status] || item.description_status}
                      </span>
                    )}
                  </button>
                  <button
                    type="button"
                    className={`gal-card-pin ${item.pinned ? "on" : ""}`.trim()}
                    onClick={() => togglePin(item)}
                    aria-pressed={Boolean(item.pinned)}
                    aria-label={item.pinned ? "Unpin" : "Pin"}
                    title={item.pinned ? "Unpin" : "Pin"}
                  >
                    <Icon name="pin" />
                  </button>
                  <div className="gal-card-foot">
                    <span className="gal-card-name">{item.title || "Untitled"}</span>
                    <span className="gal-card-meta">{relativeDate(item.created_at)}</span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {dropping && (
          <div className="gal-dropzone">
            <Icon name="upload" />
            Drop to add
          </div>
        )}
      </div>

      {selected && (
        <aside className="gal-panel" aria-label="Image details">
          <div className="gal-panel-head">
            <div className="gal-panel-nav">
              <button type="button" onClick={() => step(-1)} disabled={index <= 0} aria-label="Previous image">
                <Icon name="back" />
              </button>
              <span>{index >= 0 ? `${index + 1} of ${ordered.length}` : ""}</span>
              <button type="button" onClick={() => step(1)} disabled={index < 0 || index >= ordered.length - 1} aria-label="Next image">
                <Icon name="next" />
              </button>
            </div>
            <button className="gal-panel-close" type="button" onClick={() => setSelectedId(null)} aria-label="Close details">
              <Icon name="close" />
            </button>
          </div>

          <div className="gal-panel-body">
            <button className="gal-preview" type="button" onClick={() => setZoomed(true)} title="Open full size">
              <img
                src={api.galleryImageUrl(selected.id)}
                alt={selected.alt_text || selected.title || "Gallery image"}
              />
              <span className="gal-preview-zoom"><Icon name="expand" /></span>
            </button>

            <h2 className="gal-panel-title">{selected.title || "Untitled"}</h2>
            <div className="gal-facts">
              {dimensions(selected) && <span>{dimensions(selected)}</span>}
              {selected.image_format && <span>{selected.image_format.toUpperCase()}</span>}
              <span>{ORIGIN_LABEL[selected.origin] || selected.origin}</span>
              <span>{shortDate(selected.created_at)}</span>
            </div>

            <div className="gal-panel-actions">
              <button className="gal-btn" type="button" disabled={busy} onClick={redescribe}>
                <Icon name="sparkle" />
                Describe again
              </button>
              <button
                className={`gal-btn ${selected.pinned ? "on" : ""}`.trim()}
                type="button"
                disabled={busy}
                onClick={() => togglePin(selected)}
              >
                <Icon name="pin" />
                {selected.pinned ? "Pinned" : "Pin"}
              </button>
              {!draft && (
                <button className="gal-btn" type="button" onClick={() => setDraft({
                  title: selected.title || "",
                  caption: selected.caption || "",
                  alt_text: selected.alt_text || "",
                  tags: (selected.tags || []).join(", "),
                })}>
                  <Icon name="write" />
                  Edit
                </button>
              )}
            </div>

            {draft ? (
              <section className="gal-section">
                <h3>Edit</h3>
                <label className="gal-field">
                  <span>Title</span>
                  <input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} />
                </label>
                <label className="gal-field">
                  <span>Description</span>
                  <textarea rows={5} value={draft.caption} onChange={(event) => setDraft({ ...draft, caption: event.target.value })} />
                </label>
                <label className="gal-field">
                  <span>Alt text</span>
                  <input value={draft.alt_text} onChange={(event) => setDraft({ ...draft, alt_text: event.target.value })} />
                </label>
                <label className="gal-field">
                  <span>Tags</span>
                  <input value={draft.tags} placeholder="comma, separated"
                    onChange={(event) => setDraft({ ...draft, tags: event.target.value })} />
                </label>
                <div className="gal-panel-actions">
                  <button className="gal-btn primary" type="button" disabled={busy} onClick={save}>Save</button>
                  <button className="gal-btn" type="button" onClick={() => setDraft(null)}>Cancel</button>
                </div>
              </section>
            ) : (
              <section className="gal-section">
                <h3>
                  Description
                  <em className={`gal-state ${selected.description_status}`}>
                    {STATUS_LABEL[selected.description_status] || selected.description_status}
                  </em>
                </h3>
                <p className="gal-caption">{selected.caption || "Not described yet."}</p>
                {selected.description_error && (
                  <p className="gal-error inline"><Icon name="warning" />{selected.description_error}</p>
                )}
                {(selected.tags || []).length > 0 && (
                  <div className="gal-chiprow">
                    {selected.tags.map((name) => (
                      <button key={name} type="button" className="gal-chip" onClick={() => setTag(name)}>
                        {name}
                      </button>
                    ))}
                  </div>
                )}
                {selected.user_edited && (
                  <p className="gal-hint">Edited by you — describing again keeps your words.</p>
                )}
              </section>
            )}

            {selected.ocr_text && (
              <section className="gal-section">
                <h3>
                  Text in this image
                  <button className="gal-linkbtn" type="button" onClick={() => setShowOcr((value) => !value)}>
                    {showOcr ? "Hide" : "Show"}
                  </button>
                </h3>
                {showOcr && <pre className="gal-ocr">{selected.ocr_text}</pre>}
              </section>
            )}

            <section className="gal-section">
              <h3>Where this appeared</h3>
              {appearances.length === 0 ? (
                <p className="gal-hint">Not shown in a conversation yet.</p>
              ) : (
                <ul className="gal-appearances">
                  {appearances.map((appearance) => (
                    <li key={appearance.id}>
                      <span className="gal-appearance-when">{shortDate(appearance.seen_at)}</span>
                      <span className="gal-appearance-role">{appearance.role}</span>
                      {appearance.chat_id != null && (
                        <button type="button" className="gal-linkbtn" onClick={() => onOpenChat?.(appearance.chat_id)}>
                          Open chat
                          <Icon name="external" />
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section className="gal-section">
              <h3>Reference</h3>
              <p className="gal-hint">Ask Neo about it by name, by what was in it, or by this id.</p>
              <code className="gal-id">{selected.id}</code>
            </section>
          </div>

          <div className="gal-panel-foot">
            {confirming ? (
              <>
                <span className="gal-confirm-text">Delete this image?</span>
                <button className="gal-btn" type="button" onClick={() => setConfirming(false)}>Cancel</button>
                <button className="gal-btn danger" type="button" disabled={busy} onClick={() => remove(false)}>Remove</button>
                <button className="gal-btn danger" type="button" disabled={busy} onClick={() => remove(true)}>Erase file</button>
              </>
            ) : (
              <button className="gal-btn danger" type="button" onClick={() => setConfirming(true)}>
                <Icon name="trash" />
                Delete
              </button>
            )}
          </div>
        </aside>
      )}

      {zoomed && selected && (
        <div className="gal-lightbox" role="dialog" aria-modal="true" aria-label={selected.title || "Image"}>
          <button className="gal-lightbox-scrim" type="button" onClick={() => setZoomed(false)} aria-label="Close" />
          <button className="gal-lightbox-step left" type="button" onClick={() => step(-1)} disabled={index <= 0} aria-label="Previous">
            <Icon name="back" />
          </button>
          <figure className="gal-lightbox-figure">
            <img src={api.galleryImageUrl(selected.id)} alt={selected.alt_text || selected.title || "Gallery image"} />
            <figcaption>
              <strong>{selected.title || "Untitled"}</strong>
              <span>{[dimensions(selected), shortDate(selected.created_at)].filter(Boolean).join(" · ")}</span>
            </figcaption>
          </figure>
          <button className="gal-lightbox-step right" type="button" onClick={() => step(1)} disabled={index >= ordered.length - 1} aria-label="Next">
            <Icon name="next" />
          </button>
          <button className="gal-lightbox-close" type="button" onClick={() => setZoomed(false)} aria-label="Close">
            <Icon name="close" />
          </button>
        </div>
      )}
    </div>
  );
}
