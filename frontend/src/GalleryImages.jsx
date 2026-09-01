import { useState } from "react";

import ImageLightbox from "./ImageLightbox.jsx";
import { api } from "./api.js";

/**
 * The images a turn carried, shown inside the bubble they belong to.
 *
 * Used two ways, and the difference matters. On a user turn these are the
 * images that were *shown* to Neo, so the transcript reads the way the
 * conversation actually happened rather than as a bare "(no extractable text)"
 * line. On an assistant turn they are what a gallery search *found*, which is
 * what makes "find that image I showed you last week" answer with the picture
 * instead of a filename.
 *
 * Every tile is the same size whatever the picture's shape. Letting the source
 * dimensions through made a screenshot tower over a thumbnail beside it, so the
 * strip read as a pile rather than a set; the crop is only the preview, and the
 * whole uncropped image is one click away.
 */
export default function GalleryImages({ items = [], caption = "", onOpenGallery }) {
  const [zoomed, setZoomed] = useState(-1);
  if (!items.length) return null;

  const entries = items.map((item) => {
    const id = typeof item === "string" ? item : item.id;
    const title = typeof item === "string" ? "" : item.title || "";
    return {
      id,
      title,
      alt: typeof item === "string" ? "Attached image" : item.alt_text || title || "Image",
    };
  });

  return (
    <div className="gallery-strip">
      {caption ? <p className="gallery-strip-caption">{caption}</p> : null}
      <div className="gallery-strip-row">
        {entries.map((entry, position) => (
          <figure key={entry.id} className="gallery-strip-item">
            <button
              type="button"
              className="gallery-strip-button"
              onClick={() => setZoomed(position)}
              title={entry.title ? `${entry.title} (click to enlarge)` : "Click to enlarge"}
              aria-label={entry.title ? `Enlarge ${entry.title}` : "Enlarge image"}
            >
              <img src={api.galleryThumbnailUrl(entry.id)} alt={entry.alt} loading="lazy" />
            </button>
            {entry.title ? <figcaption>{entry.title}</figcaption> : null}
          </figure>
        ))}
      </div>

      {zoomed >= 0 && (
        <ImageLightbox
          images={entries}
          index={zoomed}
          onIndexChange={setZoomed}
          onClose={() => setZoomed(-1)}
          action={
            onOpenGallery
              ? { label: "Open in the gallery", onClick: () => onOpenGallery(entries[zoomed].id) }
              : null
          }
        />
      )}
    </div>
  );
}
