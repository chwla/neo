/**
 * Pictures and files in a note.
 *
 * A picture is the official Image node with the id of the file behind it; a
 * file is a chip (name, size, a way to open it). Both are stored by Neo's
 * Files service as made by Notes (POST /notes/attachments) and referenced by
 * the note's next save, which is what keeps them; one nothing refers to any
 * more is collected later. In the Markdown mirror both are links to
 * /api/files/<id>/download, which agents and search can follow.
 *
 * Adding one (paste, drop, the toolbar) uploads it first and inserts it when
 * the upload is done. Meanwhile a placeholder sits where it will go and moves
 * with the text around it, so typing never waits for a network.
 *
 * In a locked note the file is ciphertext, and its node carries `aid`, the id
 * it was encrypted under (inside the encrypted note, like its name). Such a
 * picture or file is shown and opened through the editor's `resolveFile`,
 * which decrypts it in memory; the stored bytes are never shown as they are.
 */
import { Node, mergeAttributes } from "@tiptap/core";
import FileHandler from "@tiptap/extension-file-handler";
import Image from "@tiptap/extension-image";
import { Plugin, PluginKey } from "@tiptap/pm/state";
import { Decoration, DecorationSet } from "@tiptap/pm/view";

const FILE_URL = /\/api\/files\/([0-9a-fA-F-]{36})\/download/;

export const fileUrl = (id) => `/api/files/${id}/download`;

/** How the editor opens an encrypted attachment: a blob URL for its decrypted bytes. */
function resolverOf(editor) {
  return editor?.extensionManager?.extensions.find((e) => e.name === "noteAttachments")?.options.resolveFile ?? null;
}

const aidAttribute = {
  default: null,
  parseHTML: (element) => element.getAttribute("data-aid") || null,
  renderHTML: (attributes) => (attributes.aid ? { "data-aid": attributes.aid } : {}),
};

/** The official image, carrying the id of the file it shows. */
export const NoteImage = Image.extend({
  addAttributes() {
    return {
      ...this.parent?.(),
      fileId: {
        default: null,
        parseHTML: (element) => element.getAttribute("data-file-id") || element.getAttribute("src")?.match(FILE_URL)?.[1] || null,
        renderHTML: (attributes) => (attributes.fileId ? { "data-file-id": attributes.fileId } : {}),
      },
      aid: aidAttribute,
    };
  },

  addNodeView() {
    return ({ node, editor, HTMLAttributes }) => {
      const img = document.createElement("img");
      for (const [name, value] of Object.entries(HTMLAttributes)) {
        if (value != null && name !== "src") img.setAttribute(name, value);
      }
      let shown = null;
      const paint = (current) => {
        const { src, aid, fileId, alt } = current.attrs;
        const wanted = `${fileId}|${aid}|${src}`;
        if (wanted === shown) return;
        shown = wanted;
        if (alt) img.alt = alt;
        if (!aid) {
          img.src = src || "";
          return;
        }
        // Ciphertext is never put in front of the browser as a picture.
        img.removeAttribute("src");
        img.classList.add("nw-image-opening");
        const resolve = resolverOf(editor);
        if (!resolve) return;
        resolve(current.attrs)
          .then((url) => {
            if (shown === wanted) {
              img.src = url;
              img.classList.remove("nw-image-opening");
            }
          })
          .catch(() => {
            if (shown === wanted) img.alt = "This picture couldn't be opened.";
          });
      };
      paint(node);
      return {
        dom: img,
        update(next) {
          if (next.type.name !== "image") return false;
          paint(next);
          return true;
        },
      };
    };
  },
}).configure({ inline: false, allowBase64: false });

export function formatSize(bytes) {
  if (!Number.isFinite(bytes)) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 102.4) / 10} KB`;
  return `${Math.round(bytes / (1024 * 102.4)) / 10} MB`;
}

/** A file in a note: its name and size; tapping it opens or downloads it. */
export const FileAttachment = Node.create({
  name: "fileAttachment",
  group: "block",
  atom: true,
  draggable: true,
  selectable: true,

  addAttributes() {
    return {
      fileId: { default: null, parseHTML: (element) => element.getAttribute("data-file-id") },
      name: { default: "File", parseHTML: (element) => element.getAttribute("data-name") || element.textContent.trim() },
      size: { default: null, parseHTML: (element) => Number(element.getAttribute("data-size")) || null },
      mime: { default: null, parseHTML: (element) => element.getAttribute("data-mime") },
      aid: aidAttribute,
    };
  },

  parseHTML() {
    return [{ tag: 'div[data-type="file-attachment"]' }];
  },

  renderHTML({ node, HTMLAttributes }) {
    const { fileId, name, size, mime } = node.attrs;
    return [
      "div",
      mergeAttributes(HTMLAttributes, {
        "data-type": "file-attachment",
        "data-file-id": fileId,
        "data-name": name,
        "data-size": size ?? "",
        "data-mime": mime ?? "",
        class: "nw-file",
      }),
      ["a", { href: fileId ? fileUrl(fileId) : "#", download: name, class: "nw-file-link", contenteditable: "false" }, name],
    ];
  },

  addNodeView() {
    return ({ node, editor }) => {
      let current = node;
      const dom = document.createElement("div");
      dom.className = "nw-file";
      dom.dataset.type = "file-attachment";
      dom.contentEditable = "false";
      const link = document.createElement("a");
      link.className = "nw-file-link";
      const icon = document.createElement("span");
      icon.className = "nw-file-icon";
      icon.setAttribute("aria-hidden", "true");
      const label = document.createElement("span");
      label.className = "nw-file-text";
      const name = document.createElement("span");
      name.className = "nw-file-name";
      const meta = document.createElement("span");
      meta.className = "nw-file-meta";
      label.append(name, meta);
      link.append(icon, label);
      dom.append(link);
      const paint = (current) => {
        const { fileId, name: fileName, size, mime } = current.attrs;
        link.href = fileId ? fileUrl(fileId) : "#";
        link.download = fileName || "file";
        link.setAttribute("aria-label", `Download ${fileName}${size ? `, ${formatSize(size)}` : ""}`);
        name.textContent = fileName || "File";
        meta.textContent = [formatSize(size), (mime || "").split("/").pop()?.toUpperCase()].filter(Boolean).join(" · ");
        icon.textContent = ((fileName || "").split(".").pop() || "file").slice(0, 4).toUpperCase();
      };
      paint(node);
      // An encrypted file is opened here, decrypted, not downloaded as it is stored.
      link.addEventListener("click", (event) => {
        if (!current.attrs.aid) return;
        event.preventDefault();
        const resolve = resolverOf(editor);
        if (!resolve) return;
        resolve(current.attrs).then((url) => {
          const save = document.createElement("a");
          save.href = url;
          save.download = current.attrs.name || "file";
          document.body.append(save);
          save.click();
          save.remove();
        });
      });
      return {
        dom,
        update(next) {
          if (next.type.name !== "fileAttachment") return false;
          current = next;
          paint(next);
          return true;
        },
        // The link is the browser's to follow (it downloads): not the editor's,
        // whose link handling would also open it in a new tab. The editor
        // handles a click from its mousedown, so both are kept from it.
        stopEvent: (event) => (event.type === "click" || event.type === "mousedown") && Boolean(event.target.closest?.("a")),
        ignoreMutation: () => true,
      };
    };
  },

  renderText({ node }) {
    return node.attrs.name || "File";
  },

  renderMarkdown(node) {
    const name = (node.attrs?.name || "File").replace(/[[\]]/g, "");
    return node.attrs?.fileId ? `[${name}](${fileUrl(node.attrs.fileId)})` : name;
  },
});

/* ------------------------------------------------- uploads in progress -- */

const uploadsKey = new PluginKey("noteUploads");

/** Placeholders for uploads in flight: positions that move with the text. */
const UploadPlaceholders = new Plugin({
  key: uploadsKey,
  state: {
    init: () => DecorationSet.empty,
    apply(tr, set) {
      let next = set.map(tr.mapping, tr.doc);
      const meta = tr.getMeta(uploadsKey);
      if (meta?.add) {
        const widget = document.createElement("div");
        widget.className = "nw-uploading";
        widget.setAttribute("role", "status");
        widget.textContent = `Adding ${meta.add.name}…`;
        next = next.add(tr.doc, [Decoration.widget(meta.add.pos, widget, { id: meta.add.id, side: -1 })]);
      }
      if (meta?.remove) next = next.remove(next.find(null, null, (spec) => spec.id === meta.remove));
      return next;
    },
  },
  props: {
    decorations(state) {
      return this.getState(state);
    },
  },
});

function placeholderPos(state, id) {
  const found = uploadsKey.getState(state)?.find(null, null, (spec) => spec.id === id);
  return found?.length ? found[0].from : null;
}

/* ---------------------------------------------------------- pictures -- */

const LONG_EDGE = 2560;
const KEEP_AS_IS = new Set(["image/gif"]);

/**
 * A picture made small enough to send: at most 2560 px on its long edge and
 * under `maxBytes`, turned the right way up. Animated GIFs are left alone.
 */
export async function preparePicture(file, maxBytes) {
  if (!file.type.startsWith("image/") || KEEP_AS_IS.has(file.type) || typeof createImageBitmap !== "function") return file;
  let bitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    return file; // a format this browser cannot draw: sent as it is
  }
  const scale = Math.min(1, LONG_EDGE / Math.max(bitmap.width, bitmap.height));
  if (scale === 1 && file.size <= maxBytes) {
    bitmap.close?.();
    return file;
  }
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(bitmap.width * scale);
  canvas.height = Math.round(bitmap.height * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close?.();
  const keepPng = file.type === "image/png" && file.size <= maxBytes * 2;
  const type = keepPng ? "image/png" : "image/jpeg";
  for (const quality of [0.86, 0.78, 0.68]) {
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, type, quality));
    if (blob && (blob.size <= maxBytes || quality === 0.68)) {
      const name = keepPng ? file.name : file.name.replace(/\.[^.]+$/, "") + ".jpg";
      return new File([blob], name, { type, lastModified: file.lastModified });
    }
  }
  return file;
}

/**
 * Upload `files` and put each where `pos` was (or at the selection), however
 * the text has moved meanwhile. The editor is reused when another note opens,
 * so an upload belongs to the session token at which it started. `upload(file)`
 * resolves with the server's attachment; `onError(message)` hears about any
 * that could not be added.
 */
export async function insertAttachments(editor, files, { pos = null, upload, onError, currentNoteToken = null, maxBytes = 5 * 1024 * 1024 } = {}) {
  const originToken = currentNoteToken?.();
  const stillInOrigin = () => !currentNoteToken || currentNoteToken() === originToken;
  const switchedNotes = () => onError?.("Attachment wasn't added because you switched notes. Try again in the original note.");
  for (const original of files) {
    if (editor.isDestroyed || !editor.isEditable) return;
    if (!stillInOrigin()) {
      switchedNotes();
      return;
    }
    const id = `upload-${Math.random().toString(36).slice(2)}`;
    const at = pos ?? editor.state.selection.to;
    editor.view.dispatch(editor.state.tr.setMeta(uploadsKey, { add: { id, pos: Math.min(at, editor.state.doc.content.size), name: original.name || "attachment" } }).setMeta("addToHistory", false));
    try {
      const file = await preparePicture(original, maxBytes);
      if (!stillInOrigin()) {
        switchedNotes();
        return;
      }
      if (file.size > maxBytes) throw new Error(`${original.name || "That file"} is larger than ${formatSize(maxBytes)}.`);
      const { attachment } = await upload(file, originToken);
      if (editor.isDestroyed) return;
      if (!stillInOrigin()) {
        switchedNotes();
        return;
      }
      const where = placeholderPos(editor.state, id) ?? editor.state.selection.to;
      const aid = attachment.aid ?? null;
      const node =
        attachment.kind === "image"
          ? { type: "image", attrs: { src: attachment.url, fileId: attachment.id, alt: attachment.name, aid } }
          : { type: "fileAttachment", attrs: { fileId: attachment.id, name: attachment.name, size: attachment.size, mime: attachment.mime_type, aid } };
      editor.view.dispatch(editor.state.tr.setMeta(uploadsKey, { remove: id }).setMeta("addToHistory", false));
      editor.chain().insertContentAt(where, node).run();
    } catch (error) {
      if (!editor.isDestroyed && stillInOrigin()) {
        editor.view.dispatch(editor.state.tr.setMeta(uploadsKey, { remove: id }).setMeta("addToHistory", false));
        onError?.(error?.message || "That file couldn't be added.");
      } else if (!editor.isDestroyed) switchedNotes();
    }
  }
}

/**
 * Paste and drop of files (the official FileHandler), placeholders for uploads
 * in flight, and the upload itself, which the editor supplies.
 */
export const Attachments = FileHandler.extend({
  name: "noteAttachments",

  addOptions() {
    return {
      ...this.parent?.(),
      upload: null,
      onError: null,
      currentNoteToken: null,
      // attrs => Promise<blob URL> for an encrypted attachment (a locked note's).
      resolveFile: null,
      maxBytes: 5 * 1024 * 1024,
      consumePasteEvent: true,
      onPaste: (editor, files) => {
        const { upload, onError, currentNoteToken, maxBytes } = editor.extensionManager.extensions.find((e) => e.name === "noteAttachments").options;
        if (upload) insertAttachments(editor, files, { upload, onError, currentNoteToken, maxBytes });
      },
      onDrop: (editor, files, pos) => {
        const { upload, onError, currentNoteToken, maxBytes } = editor.extensionManager.extensions.find((e) => e.name === "noteAttachments").options;
        if (upload) insertAttachments(editor, files, { pos, upload, onError, currentNoteToken, maxBytes });
      },
    };
  },

  addProseMirrorPlugins() {
    return [...(this.parent?.() ?? []), UploadPlaceholders];
  },
});
