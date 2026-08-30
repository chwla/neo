import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "./api.js";

const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const REMINDER_PRESETS = [
  [0, "At start"],
  [5, "5 minutes before"],
  [15, "15 minutes before"],
  [30, "30 minutes before"],
  [60, "1 hour before"],
  [1440, "1 day before"],
];

function pad(value) {
  return String(value).padStart(2, "0");
}

function dateKey(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function toLocalInputValue(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return `${dateKey(date)}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function fromLocalInputValue(value) {
  if (!value) return null;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const offsetMinutes = -date.getTimezoneOffset();
  const sign = offsetMinutes >= 0 ? "+" : "-";
  const offsetHours = pad(Math.floor(Math.abs(offsetMinutes) / 60));
  const offsetRemainder = pad(Math.abs(offsetMinutes) % 60);
  return `${dateKey(date)}T${pad(date.getHours())}:${pad(date.getMinutes())}:00${sign}${offsetHours}:${offsetRemainder}`;
}

function startOfMonth(date) {
  return new Date(date.getFullYear(), date.getMonth(), 1);
}

function addMonths(date, count) {
  return new Date(date.getFullYear(), date.getMonth() + count, 1);
}

function buildMonthGrid(monthCursor) {
  const first = startOfMonth(monthCursor);
  // Monday-start weeks, matching the backend's 0=Monday `by_weekday` convention.
  const leadingDays = (first.getDay() + 6) % 7;
  const gridStart = new Date(first.getFullYear(), first.getMonth(), 1 - leadingDays);
  return Array.from({ length: 42 }, (_, index) => {
    const day = new Date(gridStart);
    day.setDate(gridStart.getDate() + index);
    return day;
  });
}

function emptyDraft(defaultStart) {
  return {
    title: "",
    description: "",
    location: "",
    start_at: defaultStart ? toLocalInputValue(defaultStart.toISOString()) : "",
    end_at: "",
    all_day: false,
    repeats: "none",
    interval: 1,
    by_weekday: [],
    ends: "never",
    until: "",
    count: 10,
    reminders: [],
  };
}

function toDraft(event) {
  if (!event) return emptyDraft();
  const recurrence = event.recurrence || null;
  return {
    title: event.title || "",
    description: event.description || "",
    location: event.location || "",
    start_at: toLocalInputValue(event.start_at),
    end_at: event.end_at ? toLocalInputValue(event.end_at) : "",
    all_day: Boolean(event.all_day),
    repeats: recurrence?.freq || "none",
    interval: recurrence?.interval || 1,
    by_weekday: recurrence?.by_weekday || [],
    ends: recurrence?.until ? "on" : recurrence?.count ? "after" : "never",
    until: recurrence?.until || "",
    count: recurrence?.count || 10,
    reminders: event.reminder_minutes_before || [],
  };
}

function buildPayload(draft) {
  const start_at = fromLocalInputValue(draft.start_at);
  if (!start_at) {
    throw new Error("Start date/time is required.");
  }
  let recurrence = null;
  if (draft.repeats !== "none") {
    recurrence = {
      freq: draft.repeats,
      interval: Math.max(1, Number(draft.interval) || 1),
      by_weekday:
        draft.repeats === "weekly" && draft.by_weekday.length ? draft.by_weekday : null,
      until: draft.ends === "on" && draft.until ? draft.until : null,
      count: draft.ends === "after" ? Math.max(1, Number(draft.count) || 1) : null,
    };
  }
  return {
    title: draft.title.trim(),
    description: draft.description,
    location: draft.location,
    start_at,
    end_at: draft.end_at ? fromLocalInputValue(draft.end_at) : null,
    all_day: draft.all_day,
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
    recurrence,
    reminder_minutes_before: draft.reminders,
  };
}

function formatMonthLabel(date) {
  return date.toLocaleDateString([], { month: "long", year: "numeric" });
}

function formatEventTime(occurrence) {
  if (occurrence.all_day) return "All day";
  const date = new Date(occurrence.occurrence_start);
  return Number.isNaN(date.getTime())
    ? ""
    : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export default function Calendar({ initialEventId = null, onBack }) {
  const [monthCursor, setMonthCursor] = useState(() => startOfMonth(new Date()));
  const [occurrences, setOccurrences] = useState([]);
  const [selectedDay, setSelectedDay] = useState(() => dateKey(new Date()));
  const [selectedEvent, setSelectedEvent] = useState(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => emptyDraft());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const grid = useMemo(() => buildMonthGrid(monthCursor), [monthCursor]);

  const load = useCallback(async () => {
    const start = grid[0];
    const end = grid[grid.length - 1];
    const rangeEnd = new Date(end.getFullYear(), end.getMonth(), end.getDate() + 1);
    try {
      const data = await api.calendarEventsList(start.toISOString(), rangeEnd.toISOString());
      setOccurrences(data.events || []);
    } catch (err) {
      setError(err.message || "Failed to load calendar events.");
    }
  }, [grid]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!initialEventId) return;
    api
      .calendarEvent(initialEventId)
      .then((data) => {
        setSelectedEvent(data.event);
        setDraft(toDraft(data.event));
        setEditing(true);
      })
      .catch(() => {});
  }, [initialEventId]);

  const occurrencesByDay = useMemo(() => {
    const map = {};
    for (const occurrence of occurrences) {
      const key = dateKey(new Date(occurrence.occurrence_start));
      (map[key] ||= []).push(occurrence);
    }
    return map;
  }, [occurrences]);

  const dayOccurrences = occurrencesByDay[selectedDay] || [];

  function openNewEvent(day) {
    const start = new Date(day || selectedDay);
    start.setHours(9, 0, 0, 0);
    setSelectedEvent(null);
    setDraft(emptyDraft(start));
    setEditing(true);
    setError("");
  }

  async function openOccurrence(occurrence) {
    setBusy(true);
    setError("");
    try {
      const data = await api.calendarEvent(occurrence.id);
      setSelectedEvent(data.event);
      setDraft(toDraft(data.event));
      setEditing(true);
    } catch (err) {
      setError(err.message || "Failed to load event.");
    } finally {
      setBusy(false);
    }
  }

  function toggleWeekday(day) {
    setDraft((current) => ({
      ...current,
      by_weekday: current.by_weekday.includes(day)
        ? current.by_weekday.filter((item) => item !== day)
        : [...current.by_weekday, day].sort(),
    }));
  }

  function toggleReminder(minutes) {
    setDraft((current) => ({
      ...current,
      reminders: current.reminders.includes(minutes)
        ? current.reminders.filter((item) => item !== minutes)
        : [...current.reminders, minutes].sort((a, b) => a - b),
    }));
  }

  async function saveEvent(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const payload = buildPayload(draft);
      if (selectedEvent) {
        await api.updateCalendarEvent(selectedEvent.id, payload);
      } else {
        await api.createCalendarEvent(payload);
      }
      setEditing(false);
      setSelectedEvent(null);
      await load();
    } catch (err) {
      setError(err.message || "Failed to save event.");
    } finally {
      setBusy(false);
    }
  }

  async function removeEvent() {
    if (!selectedEvent) return;
    if (!window.confirm("Delete this event?")) return;
    setBusy(true);
    setError("");
    try {
      await api.deleteCalendarEvent(selectedEvent.id);
      setEditing(false);
      setSelectedEvent(null);
      await load();
    } catch (err) {
      setError(err.message || "Failed to delete event.");
    } finally {
      setBusy(false);
    }
  }

  const today = dateKey(new Date());

  return (
    <main className="calendar-layout">
      <section className="calendar-grid-pane">
        <div className="tasks-pane-header">
          <button className="neo-button secondary" type="button" onClick={onBack}>
            Back
          </button>
          <h2>Calendar</h2>
          <button className="neo-button" type="button" onClick={() => openNewEvent()}>
            New Event
          </button>
        </div>

        <div className="calendar-toolbar">
          <button
            className="neo-button secondary"
            type="button"
            onClick={() => setMonthCursor((current) => addMonths(current, -1))}
          >
            ‹
          </button>
          <strong className="calendar-month-label">{formatMonthLabel(monthCursor)}</strong>
          <button
            className="neo-button secondary"
            type="button"
            onClick={() => setMonthCursor((current) => addMonths(current, 1))}
          >
            ›
          </button>
          <button
            className="neo-button secondary"
            type="button"
            onClick={() => {
              setMonthCursor(startOfMonth(new Date()));
              setSelectedDay(today);
            }}
          >
            Today
          </button>
        </div>

        {error ? <div className="task-error">{error}</div> : null}

        <div className="calendar-weekday-row">
          {WEEKDAY_LABELS.map((label) => (
            <div key={label} className="calendar-weekday-cell">
              {label}
            </div>
          ))}
        </div>
        <div className="calendar-month-grid">
          {grid.map((day) => {
            const key = dateKey(day);
            const inMonth = day.getMonth() === monthCursor.getMonth();
            const items = occurrencesByDay[key] || [];
            return (
              <button
                type="button"
                key={key}
                className={`calendar-day-cell${inMonth ? "" : " outside-month"}${key === selectedDay ? " selected" : ""}${key === today ? " is-today" : ""}`}
                onClick={() => setSelectedDay(key)}
                onDoubleClick={() => openNewEvent(day)}
              >
                <span className="calendar-day-number">{day.getDate()}</span>
                <div className="calendar-day-events">
                  {items.slice(0, 3).map((occurrence) => (
                    <span
                      key={`${occurrence.id}-${occurrence.occurrence_start}`}
                      className={`calendar-event-pill ${occurrence.source === "neo" ? "neo" : "user"}`}
                    >
                      {occurrence.title}
                    </span>
                  ))}
                  {items.length > 3 ? (
                    <span className="calendar-event-overflow">+{items.length - 3} more</span>
                  ) : null}
                </div>
              </button>
            );
          })}
        </div>
      </section>

      <section className="calendar-detail-pane">
        {editing ? (
          <form className="calendar-editor" onSubmit={saveEvent}>
            <input
              className="task-title-input"
              value={draft.title}
              maxLength={200}
              onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              placeholder="Event title"
              autoFocus
            />
            <textarea
              value={draft.description}
              onChange={(e) => setDraft({ ...draft, description: e.target.value })}
              placeholder="Description"
            />
            <input
              value={draft.location}
              onChange={(e) => setDraft({ ...draft, location: e.target.value })}
              placeholder="Location"
            />
            <div className="task-field-grid">
              <label>
                Starts
                <input
                  type="datetime-local"
                  value={draft.start_at}
                  onChange={(e) => setDraft({ ...draft, start_at: e.target.value })}
                  required
                />
              </label>
              <label>
                Ends
                <input
                  type="datetime-local"
                  value={draft.end_at}
                  onChange={(e) => setDraft({ ...draft, end_at: e.target.value })}
                />
              </label>
            </div>
            <label className="tasks-check">
              <input
                type="checkbox"
                checked={draft.all_day}
                onChange={(e) => setDraft({ ...draft, all_day: e.target.checked })}
              />{" "}
              All day
            </label>

            <div className="calendar-recurrence">
              <label>
                Repeats
                <select
                  value={draft.repeats}
                  onChange={(e) => setDraft({ ...draft, repeats: e.target.value })}
                >
                  <option value="none">Does not repeat</option>
                  <option value="daily">Daily</option>
                  <option value="weekly">Weekly</option>
                  <option value="monthly">Monthly</option>
                </select>
              </label>
              {draft.repeats !== "none" ? (
                <>
                  <label>
                    Every
                    <input
                      type="number"
                      min={1}
                      max={365}
                      value={draft.interval}
                      onChange={(e) => setDraft({ ...draft, interval: e.target.value })}
                    />
                  </label>
                  {draft.repeats === "weekly" ? (
                    <div className="calendar-weekday-picker">
                      {WEEKDAY_LABELS.map((label, index) => (
                        <label key={label} className="calendar-weekday-toggle">
                          <input
                            type="checkbox"
                            checked={draft.by_weekday.includes(index)}
                            onChange={() => toggleWeekday(index)}
                          />
                          {label}
                        </label>
                      ))}
                    </div>
                  ) : null}
                  <label>
                    Ends
                    <select
                      value={draft.ends}
                      onChange={(e) => setDraft({ ...draft, ends: e.target.value })}
                    >
                      <option value="never">Never</option>
                      <option value="on">On date</option>
                      <option value="after">After N times</option>
                    </select>
                  </label>
                  {draft.ends === "on" ? (
                    <input
                      type="date"
                      value={draft.until}
                      onChange={(e) => setDraft({ ...draft, until: e.target.value })}
                    />
                  ) : null}
                  {draft.ends === "after" ? (
                    <input
                      type="number"
                      min={1}
                      max={730}
                      value={draft.count}
                      onChange={(e) => setDraft({ ...draft, count: e.target.value })}
                    />
                  ) : null}
                </>
              ) : null}
            </div>

            <div className="calendar-reminders">
              <div className="task-section-title">Reminders</div>
              {REMINDER_PRESETS.map(([minutes, label]) => (
                <label key={minutes} className="tasks-check">
                  <input
                    type="checkbox"
                    checked={draft.reminders.includes(minutes)}
                    onChange={() => toggleReminder(minutes)}
                  />{" "}
                  {label}
                </label>
              ))}
            </div>

            <div className="task-actions">
              <button className="neo-button" type="submit" disabled={busy || !draft.title.trim()}>
                Save
              </button>
              <button
                className="neo-button secondary"
                type="button"
                onClick={() => {
                  setEditing(false);
                  setSelectedEvent(null);
                }}
              >
                Cancel
              </button>
              {selectedEvent ? (
                <button className="neo-button danger" type="button" onClick={removeEvent}>
                  Delete
                </button>
              ) : null}
            </div>
          </form>
        ) : (
          <div className="calendar-agenda">
            <div className="tasks-pane-header">
              <h3>{new Date(`${selectedDay}T00:00:00`).toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" })}</h3>
              <button className="neo-button" type="button" onClick={() => openNewEvent(new Date(`${selectedDay}T00:00:00`))}>
                Add
              </button>
            </div>
            {dayOccurrences.length === 0 ? (
              <p className="tasks-empty editor">Nothing on the calendar this day.</p>
            ) : (
              <div className="calendar-agenda-list">
                {dayOccurrences.map((occurrence) => (
                  <button
                    type="button"
                    key={`${occurrence.id}-${occurrence.occurrence_start}`}
                    className="calendar-agenda-row"
                    onClick={() => openOccurrence(occurrence)}
                  >
                    <span className="calendar-agenda-time">{formatEventTime(occurrence)}</span>
                    <span className="calendar-agenda-title">{occurrence.title}</span>
                    {occurrence.is_recurring_instance ? (
                      <span className="calendar-agenda-badge">repeats</span>
                    ) : null}
                    {occurrence.source === "neo" ? (
                      <span className="calendar-agenda-badge neo">added by Neo</span>
                    ) : null}
                  </button>
                ))}
              </div>
            )}
          </div>
        )}
      </section>
    </main>
  );
}
