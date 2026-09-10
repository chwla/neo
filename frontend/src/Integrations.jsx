import { useCallback, useEffect, useState } from "react";

import { api } from "./api.js";
import { Modal } from "./App.jsx";

/**
 * Settings > Connected accounts: linking a real Google account to Neo.
 *
 * Two things shape this screen more than any layout decision.
 *
 * **Permission is asked for in pieces, not once.** Connecting an account is not
 * a single yes. Each capability is its own checkbox and its own scope request,
 * so someone who wants Neo to see their calendar is never asked about their
 * mail. That is why the connect flow starts from a set of checkboxes rather
 * than a button, and why an existing connection can be reconnected to add one.
 *
 * **Nothing sensitive is available to render.** The backend builds every
 * connection object from an allowlist, so there is no token, client secret or
 * refresh value in the props here -- not hidden, absent. The panel could not
 * leak one if it tried, which is a better guarantee than remembering not to.
 *
 * The states come from `/integrations/connections`, and the flow leaves the
 * page entirely: the provider takes over the tab and its callback returns to
 * `/?integration=connected`. So there is no polling loop here as there is in
 * ExternalAgentSetup -- the answer arrives as a page load, and App.jsx reads
 * that query flag.
 */

/** Ordered by what has to be fixed first; these are not interchangeable. */
export function connectionState(row) {
  if (!row) return "absent";
  if (row.status === "needs_reauth") return "needs_reauth";
  if (row.status === "revoked") return "revoked";
  return "connected";
}

/**
 * What a provider's row can offer right now.
 *
 * `connectable` is false when this install ships no built-in OAuth client --
 * a supported state, not an error, and one worth saying out loud so the missing
 * Connect button reads as a configuration fact rather than a bug.
 */
export function providerState(provider, connections) {
  if (!provider?.connectable) return "needs_client";
  return (connections || []).some((row) => row.provider === provider.id)
    ? "linked"
    : "connectable";
}

const STATE_LABEL = {
  connected: "Connected",
  needs_reauth: "Reconnect needed",
  revoked: "Disconnected at Google",
  absent: "Not connected",
};

/** A grade the user should look twice at before granting. */
const GRADE_NOTE = {
  external: "Other people can see the result. Neo always asks first.",
};

/**
 * One checkbox per capability, with the wording the person is deciding on.
 *
 * Exported so the suite can render it with real capabilities: the panel itself
 * fetches on mount, and `renderToStaticMarkup` runs no effects, so testing the
 * loaded state through the panel would mean testing an empty one.
 */
export function CapabilityPicker({ capabilities, chosen, onToggle, disabled }) {
  return (
    <ul className="integration-capabilities">
      {capabilities.map((capability) => (
        <li key={capability.id}>
          <label>
            <input
              type="checkbox"
              checked={chosen.includes(capability.id)}
              disabled={disabled}
              onChange={() => onToggle(capability.id)}
            />
            <span className="integration-capability-label">{capability.label}</span>
          </label>
          <p className="integration-capability-description">
            {capability.description}
            {GRADE_NOTE[capability.grade] ? ` ${GRADE_NOTE[capability.grade]}` : ""}
          </p>
        </li>
      ))}
    </ul>
  );
}

/**
 * One connected account, as the panel shows it.
 *
 * Exported for the same reason as `CapabilityPicker`, and it carries the
 * property worth pinning most: the only fields it can read are the ones the
 * backend's allowlist permits. There is no token here to leak because there is
 * no token in `row`.
 */
export function ConnectionRow({ row, busy, onSetSync, onDisconnect }) {
  const state = connectionState(row);
  return (
    <section className="engine-row">
      <div className="engine-row-head">
        <strong>{row.account_email}</strong>
        <span className={`engine-state engine-state-${state}`}>{STATE_LABEL[state]}</span>
      </div>
      <p className="engine-connect-muted">
        {row.capabilities.length
          ? `Allowed to: ${row.capabilities.join(", ")}`
          : "No permissions granted."}
      </p>
      {state === "needs_reauth" ? (
        <p className="engine-connect-muted">
          Google stopped accepting the stored key. Disconnect and connect again.
        </p>
      ) : null}
      <label className="chat-tools-row">
        <input
          type="checkbox"
          className="chat-tools-toggle"
          checked={row.sync_enabled}
          disabled={busy}
          onChange={(event) => onSetSync(row.id, event.target.checked)}
        />
        <span>
          Check this account in the background
          <small className="engine-connect-muted">
            {" "}
            Off by default. With it off, Neo only looks when you ask it to.
          </small>
        </span>
      </label>
      <button type="button" className="engine-button" onClick={() => onDisconnect(row.id)} disabled={busy}>
        Disconnect
      </button>
    </section>
  );
}


/**
 * Registering the OAuth client, from inside the app.
 *
 * Neo is installed rather than hosted, so it has no client of its own to
 * identify with: whoever installs it supplies one. That used to mean editing an
 * env file and restarting a container, which is a strange thing to ask of
 * somebody whose actual goal was to press a button, so it happens here instead.
 *
 * The redirect URI is shown rather than documented because it has to be
 * registered with the provider character for character, and a value you can
 * select and copy beats one retyped out of a README.
 */
export function ClientSetup({ provider, busy, onSave, onClear }) {
  const [clientId, setClientId] = useState(provider.client_id || "");
  const [clientSecret, setClientSecret] = useState("");
  const configured = provider.client_mode === "byo";

  return (
    <div className="integration-setup">
      <ol className="integration-setup-steps">
        <li>
          Create a project at <code>console.cloud.google.com</code> and enable the
          Calendar, Docs and Drive APIs.
        </li>
        <li>
          On the OAuth consent screen, choose <strong>External</strong> and add your own
          Google address as a test user.
        </li>
        <li>
          Under Credentials, create an OAuth client of type <strong>Desktop app</strong>,
          registering this redirect URI exactly:
          <code className="integration-redirect">{provider.redirect_uri}</code>
        </li>
        <li>Paste the client ID and secret below.</li>
      </ol>

      <label className="integration-field">
        <span>Client ID</span>
        <input
          type="text"
          value={clientId}
          disabled={busy}
          spellCheck={false}
          autoComplete="off"
          placeholder="000000000000-xxxxxxxx.apps.googleusercontent.com"
          onChange={(event) => setClientId(event.target.value)}
        />
      </label>

      <label className="integration-field">
        <span>Client secret</span>
        <input
          type="password"
          value={clientSecret}
          disabled={busy}
          spellCheck={false}
          autoComplete="off"
          placeholder={configured ? "Stored. Type to replace." : ""}
          onChange={(event) => setClientSecret(event.target.value)}
        />
      </label>

      <p className="engine-connect-muted">
        Stored encrypted in this profile on this machine. A desktop client&apos;s secret is
        not confidential, which is why the sign-in is secured by PKCE instead.
      </p>

      <div className="integration-setup-actions">
        <button
          type="button"
          className="engine-button primary"
          disabled={busy || !clientId.trim()}
          onClick={() => onSave(provider.id, clientId.trim(), clientSecret)}
        >
          {configured ? "Update client" : "Save client"}
        </button>
        {configured ? (
          <button
            type="button"
            className="engine-button"
            disabled={busy}
            onClick={() => onClear(provider.id)}
          >
            Remove
          </button>
        ) : null}
      </div>
    </div>
  );
}

export default function Integrations({ onClose, backLabel, onBack }) {
  const [catalog, setCatalog] = useState({ providers: [] });
  const [connections, setConnections] = useState([]);
  const [chosen, setChosen] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [nextCatalog, nextConnections] = await Promise.all([
        api.integrationCatalog(),
        api.integrationConnections(),
      ]);
      setCatalog(nextCatalog);
      setConnections(nextConnections.connections || []);
      setError("");
    } catch (caught) {
      setError(caught.message || "Could not load your connected accounts.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const toggle = useCallback((capabilityId) => {
    setChosen((current) =>
      current.includes(capabilityId)
        ? current.filter((item) => item !== capabilityId)
        : [...current, capabilityId],
    );
  }, []);

  async function connect(providerId) {
    if (!chosen.length) {
      setError("Choose at least one thing Neo may do with the account.");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const { authorization_url: url } = await api.startIntegrationConnect(providerId, chosen);
      // The provider takes the tab from here; the callback brings it back.
      window.location.assign(url);
    } catch (caught) {
      setBusy(false);
      setError(caught.message || "Could not start connecting that account.");
    }
  }

  async function saveClient(providerId, clientId, clientSecret) {
    setBusy(true);
    setError("");
    try {
      setCatalog(await api.setIntegrationClient(providerId, clientId, clientSecret));
    } catch (caught) {
      setError(caught.message || "Could not save that OAuth client.");
    } finally {
      setBusy(false);
    }
  }

  async function clearClient(providerId) {
    setBusy(true);
    setError("");
    try {
      setCatalog(await api.clearIntegrationClient(providerId));
    } catch (caught) {
      setError(caught.message || "Could not remove that OAuth client.");
    } finally {
      setBusy(false);
    }
  }

  async function disconnect(connectionId) {
    setBusy(true);
    setError("");
    try {
      await api.disconnectIntegration(connectionId);
      await load();
    } catch (caught) {
      setError(caught.message || "Could not disconnect that account.");
    } finally {
      setBusy(false);
    }
  }

  async function setSync(connectionId, enabled) {
    setBusy(true);
    setError("");
    try {
      const updated = await api.setIntegrationSync(connectionId, enabled);
      setConnections((current) =>
        current.map((row) => (row.id === updated.id ? updated : row)),
      );
    } catch (caught) {
      setError(caught.message || "Could not change that setting.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Connected accounts" onClose={onClose} backLabel={backLabel} onBack={onBack} wide>
      <p className="dialog-caption">
        Neo keeps the keys to a connected account on this machine, in this profile, encrypted.
        They are never sent anywhere else. Anything other people would see, such as a sent message or a
        moved meeting, is shown to you for approval first, however the account is set up.
      </p>

      {error ? (
        <p className="neo-error" role="alert">
          {error}
        </p>
      ) : null}

      {loading ? <p className="engine-connect-muted">Loading…</p> : null}

      {connections.map((row) => (
        <ConnectionRow
          key={row.id}
          row={row}
          busy={busy}
          onSetSync={setSync}
          onDisconnect={disconnect}
        />
      ))}

      {catalog.providers.map((provider) => {
        const state = providerState(provider, connections);
        return (
          <section key={provider.id} className="engine-row">
            <div className="engine-row-head">
              <strong>{provider.display_name}</strong>
              <span className="engine-state">
                {state === "needs_client" ? "Setup needed" : "Add an account"}
              </span>
            </div>

            {state === "needs_client" ? (
              <>
                <p className="engine-connect-muted">
                  Neo runs on your machine, so it signs in with an OAuth client that belongs
                  to you rather than one shipped with the app. This is a one-time setup.
                </p>
                <ClientSetup
                  provider={provider}
                  busy={busy}
                  onSave={saveClient}
                  onClear={clearClient}
                />
              </>
            ) : (
              <>
                {provider.client_mode === "byo" ? (
                  <p className="engine-connect-muted">
                    Using your own OAuth client ({provider.client_id}).{" "}
                    <button
                      type="button"
                      className="integration-link-button"
                      disabled={busy}
                      onClick={() => clearClient(provider.id)}
                    >
                      Remove
                    </button>
                  </p>
                ) : null}
                <p className="engine-connect-muted">Choose what Neo may do before you sign in.</p>
                <CapabilityPicker
                  capabilities={provider.capabilities}
                  chosen={chosen}
                  onToggle={toggle}
                  disabled={busy}
                />
                <button
                  type="button"
                  className="engine-button primary"
                  onClick={() => connect(provider.id)}
                  disabled={busy || !chosen.length}
                >
                  Connect {provider.display_name}
                </button>
              </>
            )}
          </section>
        );
      })}
    </Modal>
  );
}
