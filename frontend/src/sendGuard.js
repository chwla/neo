export function createRequestId() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/**
 * Single-flight guard for chat submission.
 *
 * The composer's `sending` flag is React state, so it does not read as true until the
 * next render. Two clicks landing in the same tick therefore both saw it false and each
 * started its own request with its own idempotency key, which is what produced two user
 * rows. This guard is a plain object held in a ref: `begin` flips it synchronously, so
 * the second caller in that same tick is refused before any request goes out.
 *
 * One accepted submission owns one request id for its whole life, so a duplicate that
 * somehow slipped through would reuse the key and the backend would collapse it rather
 * than create a second turn.
 */
export function createSendGuard({ createId = createRequestId } = {}) {
  let locked = false;
  let requestId = null;

  return {
    /** Claim the in-flight slot, returning its request id, or null when already held. */
    begin() {
      if (locked) {
        return null;
      }
      locked = true;
      requestId = requestId ?? createId();
      return requestId;
    },
    /** Release the slot once the submission has finished, failed, or been cancelled. */
    release() {
      locked = false;
      requestId = null;
    },
    get isLocked() {
      return locked;
    },
    get requestId() {
      return requestId;
    },
  };
}
