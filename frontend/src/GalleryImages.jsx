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
 */
export default function GalleryImages({ items = [], caption = "", onOpenGallery }) {
  if (!items.length) return null;
  return (
    <div className="gallery-strip">
      {caption ? <p className="gallery-strip-caption">{caption}</p> : null}
      <div className="gallery-strip-row">
        {items.map((item) => {
          const id = typeof item === "string" ? item : item.id;
          const label = typeof item === "string" ? "" : item.title || "";
          const alt = typeof item === "string" ? "Attached image" : item.alt_text || label || "Image";
          return (
            <figure key={id} className="gallery-strip-item">
              <button
                type="button"
                className="gallery-strip-button"
                onClick={() => onOpenGallery?.(id)}
                title={label || "Open in the gallery"}
              >
                <img src={api.galleryThumbnailUrl(id)} alt={alt} loading="lazy" />
              </button>
              {label ? <figcaption>{label}</figcaption> : null}
            </figure>
          );
        })}
      </div>
    </div>
  );
}
