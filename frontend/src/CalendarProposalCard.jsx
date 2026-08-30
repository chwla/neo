import { useState } from "react";

import { api } from "./api.js";

function formatWhen(startAt, endAt, allDay) {
  if (!startAt) return "";
  const start = new Date(startAt);
  if (Number.isNaN(start.getTime())) return startAt;
  if (allDay) {
    return `${start.toLocaleDateString([], { dateStyle: "medium" })} (all day)`;
  }
  const when = start.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  if (!endAt) return when;
  const end = new Date(endAt);
  if (Number.isNaN(end.getTime())) return when;
  // A block of one day reads as a span; one that runs past midnight has to
  // name the second day or "16:00-01:00" looks like it ends before it starts.
  const sameDay = start.toDateString() === end.toDateString();
  return sameDay
    ? `${when} \u2013 ${end.toLocaleTimeString([], { timeStyle: "short" })}`
    : `${when} \u2013 ${end.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })}`;
}

const VERB = { create: "Add", update: "Update", delete: "Remove" };
const DONE = { create: "Added.", update: "Updated.", delete: "Removed." };

function statusFor(proposal) {
  if (proposal?.status === "approved") return "done";
  if (proposal?.status === "declined") return "declined";
  return "idle";
}

/**
 * The chat-side half of "Neo asks before touching the calendar": a draft Neo
 * proposed, shown inline in its reply. Clicking Approve is the permission
 * check, the same way an Agent Mode ApprovalCard's "Allow once" is (see
 * AgentTurn.jsx) -- and it is the only one. There is no typed equivalent;
 * saying "yes" in the chat gets pointed back here.
 *
 * The decision is server state, not React state. Approve and Decline resolve
 * the proposal message itself, so what the card shows is whatever was
 * persisted -- which is why it now survives switching to the Calendar view
 * and back, and a reload. Seeding `useState` from `proposal.status` is the
 * whole of that: the buttons come back only while the server still says the
 * proposal is unresolved.
 */
export default function CalendarProposalCard({ proposal, messageId, onResolved, onOpenCalendar }) {
  const [status, setStatus] = useState(() => statusFor(proposal));
  const [error, setError] = useState("");

  if (!proposal || !proposal.action) return null;
  const { action, event_title: eventTitle, draft } = proposal;
  const title = draft?.title || eventTitle || "that event";
  // Set when the server carried the change out but had something to say about
  // it -- a verification mismatch, most often.
  const note = proposal.resolution_note;

  async function resolve(call) {
    setStatus("busy");
    setError("");
    try {
      const result = await call();
      setStatus(statusFor(result.proposal));
      onResolved?.(messageId, result.proposal);
    } catch (err) {
      setStatus("error");
      setError(err.message || "Failed to update the calendar.");
    }
  }

  const approve = () => resolve(() => api.approveCalendarProposal(messageId));
  const decline = () => resolve(() => api.declineCalendarProposal(messageId));

  return (
    <div className="calendar-proposal-card" role="group" aria-label="Calendar proposal">
      <div className="calendar-proposal-title">
        {VERB[action] || "Update"} <strong>{title}</strong>
      </div>
      {draft?.start_at ? (
        <div className="calendar-proposal-when">
          {formatWhen(draft.start_at, draft.end_at, draft.all_day)}
        </div>
      ) : null}
      {status === "idle" ? (
        <div className="calendar-proposal-actions">
          <button className="neo-button" type="button" onClick={approve}>
            Approve
          </button>
          <button className="neo-button secondary" type="button" onClick={decline}>
            Decline
          </button>
        </div>
      ) : status === "busy" ? (
        <div className="calendar-proposal-status">Updating your calendar…</div>
      ) : status === "done" ? (
        <div className="calendar-proposal-status success">
          {DONE[action] || "Done."}{" "}
          {onOpenCalendar ? (
            <button className="calendar-proposal-link" type="button" onClick={onOpenCalendar}>
              Open Calendar
            </button>
          ) : null}
          {note ? <div className="calendar-proposal-note">{note}</div> : null}
        </div>
      ) : status === "declined" ? (
        <div className="calendar-proposal-status">Not added.</div>
      ) : (
        <div className="calendar-proposal-status error">
          {error}
          <button
            className="neo-button secondary"
            type="button"
            onClick={() => setStatus(statusFor(proposal))}
          >
            Try again
          </button>
        </div>
      )}
    </div>
  );
}
