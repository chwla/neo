/**
 * Voice input's settings screen: what is missing, and the one button that fixes it.
 *
 * This screen exists because the alternative is a greyed-out menu entry that never
 * explains itself. Every unavailable state here names what is wrong in a sentence and
 * offers the action that resolves it, rather than showing an error code.
 *
 * The vocabulary is deliberately the user's rather than the implementation's. There is
 * a "voice engine" and a "transcription model", not faster-whisper and CTranslate2 --
 * the names of the libraries are of no use to somebody who wants to talk to their
 * computer, and putting them on screen invites people to debug software they did not
 * choose and cannot change from here.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "./api.js";
import { captureSupport } from "./voice/recorder.js";

function formatMb(bytes) {
  if (!bytes) return "";
  const mb = bytes / (1024 * 1024);
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

/** One row of the "is this working" summary at the top. */
function StatusRow({ label, state, detail }) {
  return (
    <div className="voice-status-row">
      <span className={`voice-status-dot is-${state}`} aria-hidden="true" />
      <span className="voice-status-label">{label}</span>
      <span className="voice-status-detail">{detail}</span>
    </div>
  );
}

export default function VoiceSettings({ status, onStatusChange, onClose }) {
  const [models, setModels] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const [progress, setProgress] = useState(null);
  const [error, setError] = useState("");
  const [mic, setMic] = useState(null);
  const abortRef = useRef(null);
  const aliveRef = useRef(true);

  useEffect(() => () => { aliveRef.current = false; abortRef.current?.abort(); }, []);

  const refresh = useCallback(async () => {
    try {
      const [nextModels, nextStatus] = await Promise.all([api.voiceModels(), api.voiceStatus()]);
      if (!aliveRef.current) return;
      setModels(nextModels);
      onStatusChange?.(nextStatus);
    } catch (caught) {
      if (aliveRef.current) setError(caught.message || String(caught));
    }
  }, [onStatusChange]);

  useEffect(() => { refresh(); }, [refresh]);

  /* Whether the browser will even offer a microphone is a client-side fact the server
     cannot know -- a page served over plain HTTP to a LAN address has no microphone at
     all, and that is worth saying here rather than leaving as a button that does
     nothing. Permission state is read without prompting: asking for the microphone
     just to draw a settings row would be a surprising thing for a settings screen to
     do. */
  useEffect(() => {
    const support = captureSupport();
    if (!support.supported) {
      setMic({ state: "bad", detail: support.message });
      return;
    }
    if (!navigator.permissions?.query) {
      setMic({ state: "unknown", detail: "Neo will ask when you first dictate." });
      return;
    }
    navigator.permissions
      .query({ name: "microphone" })
      .then((result) => {
        if (!aliveRef.current) return;
        const detail = {
          granted: "Allowed.",
          denied: "Blocked. Allow the microphone in your browser's site settings.",
          prompt: "Neo will ask when you first dictate.",
        }[result.state];
        setMic({ state: result.state === "denied" ? "bad" : "ok", detail });
      })
      // Safari throws on names it does not know; that is not an error worth showing.
      .catch(() => setMic({ state: "unknown", detail: "Neo will ask when you first dictate." }));
  }, []);

  async function install(modelId) {
    setError("");
    setBusyId(modelId);
    setProgress({ percent: 0, message: "Starting…" });
    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await api.installVoiceModel(
        modelId,
        (event) => {
          if (!aliveRef.current) return;
          if (event.type === "progress") setProgress({ percent: event.percent ?? 0, message: event.message });
          else if (event.type === "done") setProgress({ percent: 100, message: event.message });
          else if (event.type === "error") setError(event.message || "The download failed.");
          else if (event.type === "cancelled") setProgress(null);
        },
        controller.signal,
      );
    } catch (caught) {
      if (aliveRef.current) setError(caught.message || String(caught));
    } finally {
      if (aliveRef.current) {
        setBusyId(null);
        setProgress(null);
        abortRef.current = null;
        refresh();
      }
    }
  }

  async function cancel(modelId) {
    // Two signals, because they stop different things: aborting the fetch closes the
    // browser's connection, and the endpoint stops the server-side download that
    // would otherwise carry on regardless.
    abortRef.current?.abort();
    try {
      await api.cancelVoiceModelInstall(modelId);
    } catch {
      /* the download may have finished between the click and this call */
    }
    refresh();
  }

  const engineInstalled = status?.reason !== "dependency_missing";
  const ready = Boolean(status?.available);

  return (
    <div className="voice-settings">
      <header className="voice-settings-head">
        {/* No heading: the dialog frame supplies it, and a second one would be
            announced twice by a screen reader. */}
        <p className="voice-settings-lede">
          Speak instead of typing. What you say is turned into text on this computer and
          put in the message box for you to check — nothing is sent until you send it,
          and no audio leaves the machine.
        </p>
      </header>

      <section className="voice-status" aria-label="Voice input status">
        <StatusRow
          label="Voice engine"
          state={engineInstalled ? "ok" : "bad"}
          detail={engineInstalled ? "Installed." : "Not installed."}
        />
        <StatusRow
          label="Transcription model"
          state={status?.model?.installed ? "ok" : status?.reason === "model_downloading" ? "busy" : "bad"}
          detail={
            status?.model?.installed
              ? `${status.model.label} is ready.`
              : status?.reason === "model_downloading"
                ? "Downloading…"
                : "Not downloaded."
          }
        />
        <StatusRow
          label="Microphone"
          state={mic?.state === "bad" ? "bad" : mic?.state === "ok" ? "ok" : "unknown"}
          detail={mic?.detail ?? "Checking…"}
        />
      </section>

      {!engineInstalled ? (
        /* The one case a button cannot fix. Installing a Python package into the
           server's own environment needs a restart to take effect, so pretending
           otherwise with an in-app installer would be a worse experience than saying
           plainly what to run. */
        <section className="voice-panel is-blocked">
          <h3>Voice support is not installed</h3>
          <p>
            Voice input needs an extra component that is not part of the standard
            install. Add it from a terminal, then restart Neo:
          </p>
          <pre className="voice-command">pip install -e &quot;.[voice]&quot;</pre>
          <button type="button" className="neo-button" onClick={refresh}>
            Check again
          </button>
        </section>
      ) : null}

      {engineInstalled ? (
        <section className="voice-panel">
          <h3>Transcription model</h3>
          <p className="voice-panel-lede">
            Larger models understand more, and take longer to run. You only need one.
          </p>
          {models?.models?.map((model) => {
            const downloading = busyId === model.id || model.downloading;
            return (
              <div
                key={model.id}
                className={`voice-model${model.selected ? " is-selected" : ""}`}
              >
                <div className="voice-model-head">
                  <span className="voice-model-name">
                    {model.label}
                    {model.selected ? <span className="voice-model-tag">In use</span> : null}
                  </span>
                  <span className="voice-model-size">
                    {model.installed ? formatMb(model.installed_bytes) : `~${model.approx_mb} MB`}
                  </span>
                </div>
                <p className="voice-model-detail">{model.detail}</p>

                {downloading ? (
                  <div className="voice-progress">
                    <div
                      className="voice-progress-bar"
                      role="progressbar"
                      aria-valuenow={progress?.percent ?? model.percent ?? 0}
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-label={`Downloading ${model.label}`}
                    >
                      <span style={{ width: `${progress?.percent ?? model.percent ?? 0}%` }} />
                    </div>
                    <span className="voice-progress-text">
                      {progress?.message ?? "Downloading"} · {progress?.percent ?? model.percent ?? 0}%
                    </span>
                    <button type="button" className="neo-button is-quiet" onClick={() => cancel(model.id)}>
                      Cancel
                    </button>
                  </div>
                ) : model.installed ? (
                  <span className="voice-model-ready">Downloaded</span>
                ) : (
                  <button
                    type="button"
                    className="neo-button"
                    onClick={() => install(model.id)}
                    disabled={Boolean(busyId)}
                  >
                    Download
                  </button>
                )}
              </div>
            );
          })}
          {models === null ? <p className="voice-model-detail">Loading…</p> : null}
        </section>
      ) : null}

      {error ? (
        <div className="voice-error" role="alert">
          <span>{error}</span>
          <button type="button" className="neo-button is-quiet" onClick={() => { setError(""); refresh(); }}>
            Try again
          </button>
        </div>
      ) : null}

      {ready ? (
        <p className="voice-ready-note" role="status">
          Voice input is ready. Open the <strong>+</strong> menu and choose Dictate, or
          press <kbd>⌘⇧D</kbd>.
        </p>
      ) : null}

      <footer className="voice-settings-foot">
        <button type="button" className="neo-button" onClick={onClose}>
          Done
        </button>
      </footer>
    </div>
  );
}
