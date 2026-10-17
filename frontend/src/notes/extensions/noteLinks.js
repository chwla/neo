/**
 * Links between notes, typed as ">>" and a few letters of the other note's
 * title, as in Apple Notes.
 *
 * Built on the official Mention node with a ">>" trigger. A link shows the
 * linked note's current title (renamed notes update their links), says so when
 * the note is gone, and opens it on a tap. In the Markdown mirror it is
 * [Title](note://<id>); a link of that form in a note's Markdown opens the
 * note too.
 *
 * Everything that knows about notes comes in as options, from the controller:
 * `search(query)` -> [{ id, title }], `titleOf(id)` -> { title, missing } or
 * null while unknown, `subscribe(listener)` for title changes, `onOpen(id)`.
 */
import { Extension } from "@tiptap/core";
import Mention from "@tiptap/extension-mention";
import { Plugin, PluginKey } from "@tiptap/pm/state";

export const NOTE_LINK_TRIGGER = ">>";
const NOTE_URL = /^note:\/\/([\w-]+)/;

/** The ">>" list: plain DOM by the caret, driven by the keyboard or a tap. */
function suggestionList() {
  let element = null;
  let state = { items: [], index: 0, command: null };

  function paint(props) {
    state = { ...state, items: props.items, command: props.command, index: Math.min(state.index, Math.max(0, props.items.length - 1)) };
    element.replaceChildren();
    if (!state.items.length) {
      const empty = document.createElement("div");
      empty.className = "nw-suggest-empty";
      empty.textContent = props.query ? "No notes with that title" : "Type a note's title";
      element.append(empty);
    }
    state.items.forEach((item, index) => {
      const option = document.createElement("button");
      option.type = "button";
      option.className = `nw-suggest-item${index === state.index ? " active" : ""}`;
      option.setAttribute("role", "option");
      option.setAttribute("aria-selected", String(index === state.index));
      option.textContent = item.title || "New Note";
      // Pressing a choice must not take focus out of the note being written.
      option.addEventListener("mousedown", (event) => event.preventDefault());
      option.addEventListener("click", () => state.command?.({ id: item.id, label: item.title }));
      element.append(option);
    });
    const rect = props.clientRect?.();
    if (rect) {
      const width = Math.min(300, window.innerWidth - 16);
      const left = Math.max(8, Math.min(rect.left, window.innerWidth - width - 8));
      const below = rect.bottom + 6;
      const height = element.offsetHeight || 200;
      const top = below + height > (window.visualViewport?.height ?? window.innerHeight) ? Math.max(8, rect.top - height - 6) : below;
      Object.assign(element.style, { left: `${left}px`, top: `${top}px`, width: `${width}px` });
    }
  }

  return {
    onStart(props) {
      element = document.createElement("div");
      element.className = "nw-suggest";
      element.setAttribute("role", "listbox");
      element.setAttribute("aria-label", "Link to a note");
      document.body.append(element);
      state.index = 0;
      paint(props);
    },
    onUpdate(props) {
      paint(props);
    },
    onKeyDown({ event }) {
      if (!state.items.length) return event.key === "Enter";
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        const step = event.key === "ArrowDown" ? 1 : -1;
        state.index = (state.index + step + state.items.length) % state.items.length;
        [...element.children].forEach((child, index) => {
          child.classList.toggle("active", index === state.index);
          child.setAttribute("aria-selected", String(index === state.index));
        });
        return true;
      }
      if (event.key === "Enter" || event.key === "Tab") {
        const item = state.items[state.index];
        if (item) state.command?.({ id: item.id, label: item.title });
        return true;
      }
      return false;
    },
    onExit() {
      element?.remove();
      element = null;
    },
  };
}

export const NoteLink = Mention.extend({
  name: "noteLink",

  addOptions() {
    const parent = this.parent?.() ?? {};
    return {
      ...parent,
      search: async () => [],
      titleOf: () => null,
      subscribe: () => () => {},
      onOpen: () => {},
      HTMLAttributes: { class: "nw-note-link" },
      renderText: ({ node }) => node.attrs.label || "Note",
      renderHTML: ({ options, node }) => ["span", options.HTMLAttributes, node.attrs.label || "Note"],
      suggestion: {
        char: NOTE_LINK_TRIGGER,
        allowSpaces: true,
        // Not while an input method is composing a character.
        allow: ({ editor, state, range }) => {
          if (editor.view.composing) return false;
          const $from = state.doc.resolve(range.from);
          return Boolean($from.parent.type.contentMatch.matchType(state.schema.nodes.noteLink));
        },
        items: async ({ editor, query }) => {
          const options = editor.extensionManager.extensions.find((extension) => extension.name === "noteLink").options;
          return (await options.search(query)).slice(0, 8);
        },
        command: ({ editor, range, props }) => {
          editor
            .chain()
            .focus()
            .insertContentAt(range, [
              { type: "noteLink", attrs: { id: props.id, label: props.label, mentionSuggestionChar: NOTE_LINK_TRIGGER } },
              { type: "text", text: " " },
            ])
            .run();
        },
        render: suggestionList,
      },
    };
  },

  parseHTML() {
    return [{ tag: 'span[data-type="noteLink"]' }];
  },

  addNodeView() {
    return ({ node }) => {
      const dom = document.createElement("span");
      dom.className = "nw-note-link";
      dom.dataset.type = "noteLink";
      dom.setAttribute("role", "link");
      dom.tabIndex = -1;
      let current = node;
      const paint = () => {
        const known = this.options.titleOf(current.attrs.id);
        const title = known?.title || current.attrs.label || "Note";
        dom.textContent = title;
        dom.classList.toggle("missing", Boolean(known?.missing));
        dom.title = known?.missing ? `${title} (deleted)` : `Open ${title}`;
      };
      paint();
      const unsubscribe = this.options.subscribe(paint);
      dom.addEventListener("click", (event) => {
        event.preventDefault();
        if (!this.options.titleOf(current.attrs.id)?.missing) this.options.onOpen(current.attrs.id);
      });
      return {
        dom,
        update(next) {
          if (next.type.name !== "noteLink") return false;
          current = next;
          paint();
          return true;
        },
        ignoreMutation: () => true,
        destroy: unsubscribe,
      };
    };
  },

  // [Title](note://<id>) in the Markdown mirror; Mention's own shortcode syntax is not used.
  markdownTokenizer: { name: "noteLinkShortcode", level: "inline", start: () => -1, tokenize: () => undefined },
  renderMarkdown(node) {
    const title = (node.attrs?.label || "Note").replace(/[[\]]/g, "");
    return `[${title}](note://${node.attrs?.id})`;
  },

});

/**
 * A link of the form note://<id> (a note link written as Markdown, as the
 * mirror writes `>>` links) opens that note in place. Ahead of the Link
 * extension (priority 1000), whose click handling would hand the URL to a new
 * browser tab, where it means nothing.
 */
export const NoteUrlLinks = Extension.create({
  name: "noteUrlLinks",
  priority: 1100,

  addOptions() {
    return { onOpen: () => {} };
  },

  addProseMirrorPlugins() {
    const options = this.options;
    return [
      new Plugin({
        key: new PluginKey("noteUrlLinks"),
        props: {
          handleClick(_view, _pos, event) {
            const href = event.target?.closest?.("a[href]")?.getAttribute("href");
            const id = href?.match(NOTE_URL)?.[1];
            if (!id) return false;
            event.preventDefault();
            options.onOpen(id);
            return true;
          },
        },
      }),
    ];
  },
});
