const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "/api";

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE}${path}`, {
      headers: {
        "Content-Type": "application/json",
        ...(options.headers ?? {}),
      },
      ...options,
    });
  } catch (error) {
    throw new Error(
      `Backend API is not reachable. Start FastAPI on http://127.0.0.1:8000. Details: ${
        error.message || error
      }`,
    );
  }

  if (response.status === 204) {
    return null;
  }

  const contentType = response.headers.get("content-type") ?? "";
  const body = contentType.includes("application/json")
    ? await response.json()
    : await response.text();

  if (!response.ok) {
    const detail = typeof body === "object" && body !== null ? body.detail : body;
    if (response.status === 500 && !detail) {
      throw new Error("Backend API is not running on http://127.0.0.1:8000.");
    }
    throw new Error(detail || `Request failed with ${response.status}`);
  }

  return body;
}

export const api = {
  sidebar: () => request("/sidebar"),
  createChat: (projectId = null) =>
    request("/chats", {
      method: "POST",
      body: JSON.stringify({ project_id: projectId }),
    }),
  getChat: (chatId) => request(`/chats/${chatId}`),
  sendMessage: (chatId, prompt) =>
    request(`/chats/${chatId}/messages`, {
      method: "POST",
      body: JSON.stringify({ prompt }),
    }),
  updateChatMessage: (chatId, messageId, content) =>
    request(`/chats/${chatId}/messages/${messageId}`, {
      method: "PATCH",
      body: JSON.stringify({ content }),
    }),
  deleteChat: (chatId) => request(`/chats/${chatId}`, { method: "DELETE" }),
  createProject: (name) =>
    request("/projects", {
      method: "POST",
      body: JSON.stringify({ name }),
    }),
  deleteProject: (projectId) => request(`/projects/${projectId}`, { method: "DELETE" }),
  memory: () =>
    Promise.all([
      request("/profile"),
      request("/preferences"),
      request("/goals"),
      request("/projects"),
      request("/events"),
      request("/memories"),
    ]).then(([profile, preferences, goals, projects, events, memories]) => ({
      profile,
      preferences,
      goals,
      projects,
      events,
      memories,
    })),
  updateProfile: (id, payload) =>
    request(`/profile/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteProfile: (id) => request(`/profile/${id}`, { method: "DELETE" }),
  updatePreference: (id, payload) =>
    request(`/preferences/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deletePreference: (id) => request(`/preferences/${id}`, { method: "DELETE" }),
  updateGoal: (id, payload) =>
    request(`/goals/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteGoal: (id) => request(`/goals/${id}`, { method: "DELETE" }),
  updateProjectMemory: (id, payload) =>
    request(`/projects/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteProjectMemory: (id) => request(`/projects/${id}/memory`, { method: "DELETE" }),
  updateEvent: (id, payload) =>
    request(`/events/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteEvent: (id) => request(`/events/${id}`, { method: "DELETE" }),
  updateMemory: (id, payload) =>
    request(`/memories/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteMemory: (id) => request(`/memories/${id}`, { method: "DELETE" }),
};
