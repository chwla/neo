/**
 * Which chat the view shows, and the sidebar edits made before the server agrees.
 *
 * Kept out of the component so the rules can be tested on their own: they are
 * the whole of what stops a slow response from putting the wrong chat on screen
 * or a stale sidebar from painting over an edit that is still being saved.
 */

/**
 * Whether a thread that just arrived may go on screen.
 *
 * `view` is `{ navSeq, target, committed }`: the latest navigation's load
 * number, the chat the person last asked to see, and the load number of the
 * last thread put on screen. `load` is `{ seq, navigate, chatId }`.
 *
 *  - A navigation commits only while it is still the latest one: a click on B
 *    after a click on A means A's answer is no longer wanted, whenever it lands.
 *  - Nothing commits for a chat the view no longer wants, so a refresh of the
 *    chat being left (a turn ending, an agent starting) cannot pull the screen
 *    back from the one just clicked.
 *  - Nothing older replaces something newer: two refreshes of the same chat can
 *    come back in either order, and only the later one may win.
 */
export function shouldCommitLoad(view, load) {
  if (load.navigate && view.navSeq !== load.seq) return false;
  if (view.target !== null && view.target !== load.chatId) return false;
  return load.seq > view.committed;
}

/** The chat fields the sidebar shows, which an optimistic edit patches there too. */
export const SIDEBAR_FIELDS = ["title", "pinned", "archived"];

/**
 * The sidebar payload with one chat's edited fields applied.
 *
 * Title and pin are patched in place (the server decides order, and the next
 * read brings it). Archiving removes the row and counts it in the archive
 * heading -- only when a row was actually removed, so a payload that already
 * reflects the archive is not counted twice. Unarchiving is left to the next
 * read: where the row lands is the server's call. Lists that do not hold the
 * chat keep their identity, and an edit with nothing to apply returns the very
 * same object.
 */
export function patchSidebarChat(sidebar, chatId, values) {
  const fields = Object.keys(values).filter((field) => SIDEBAR_FIELDS.includes(field));
  if (!sidebar || !fields.length) return sidebar;
  const hide = values.archived === true;
  let removed = false;
  const patchList = (chats) => {
    let changed = false;
    const next = [];
    for (const chat of chats) {
      if (chat.id !== chatId) {
        next.push(chat);
        continue;
      }
      if (hide) {
        removed = true;
        changed = true;
        continue;
      }
      const patch = {};
      if ("title" in values && chat.title !== values.title) patch.title = values.title;
      if ("pinned" in values && chat.pinned !== values.pinned) patch.pinned = values.pinned;
      if (Object.keys(patch).length) {
        changed = true;
        next.push({ ...chat, ...patch });
      } else {
        next.push(chat);
      }
    }
    return changed ? next : chats;
  };
  const chats = sidebar.chats ? patchList(sidebar.chats) : sidebar.chats;
  let projectsChanged = false;
  const projects = (sidebar.projects || []).map((project) => {
    const nextChats = patchList(project.chats || []);
    if (nextChats === (project.chats || [])) return project;
    projectsChanged = true;
    return { ...project, chats: nextChats };
  });
  if (chats === sidebar.chats && !projectsChanged) return sidebar;
  return {
    ...sidebar,
    chats,
    projects: projectsChanged ? projects : sidebar.projects,
    ...(removed ? { archived_count: (sidebar.archived_count ?? 0) + 1 } : {}),
  };
}
