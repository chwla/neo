/**
 * Connected accounts: what the panel says, and what it cannot say.
 *
 * Three properties carry the weight, and each is a way this screen could be
 * wrong while the backend was right.
 *
 * Permission is asked for in pieces. Every capability the provider offers is
 * rendered with the wording a person is actually deciding on, and a capability
 * whose result other people can see carries a warning the read-only ones do
 * not.
 *
 * A provider with no built-in OAuth client says so in words rather than
 * offering a button that cannot work. That is a supported configuration -- a
 * fork, or a build made before the client was registered -- not a failure.
 *
 * And nothing sensitive can be rendered, because nothing sensitive is present:
 * the backend builds every connection object from an allowlist. The last group
 * pins that from this side, so a credential field added to the API later cannot
 * start appearing on screen unnoticed.
 *
 * The panel fetches on mount and `renderToStaticMarkup` runs no effects, so the
 * loaded state is tested through the two presentational components the panel is
 * built from rather than through an empty shell.
 */
import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";

import Integrations, {
  CapabilityPicker,
  ClientSetup,
  ConnectionRow,
  connectionState,
  providerState,
} from "../src/Integrations.jsx";

const CONNECTION = (overrides = {}) => ({
  id: "c1",
  provider: "google",
  account_email: "person@example.com",
  capabilities: ["calendar.read"],
  status: "connected",
  sync_enabled: false,
  expires_at: null,
  ...overrides,
});

const CAPABILITIES = [
  {
    id: "calendar.read",
    label: "Read your calendar",
    description: "See your events so Neo can answer questions about your schedule.",
    grade: "read",
    tier: "sensitive",
  },
  {
    id: "mail.send",
    label: "Send mail as you",
    description: "Send messages from your address.",
    grade: "external",
    tier: "sensitive",
  },
];

const row = (props) =>
  renderToStaticMarkup(
    createElement(ConnectionRow, { busy: false, onSetSync() {}, onDisconnect() {}, ...props }),
  );

const picker = (props) =>
  renderToStaticMarkup(
    createElement(CapabilityPicker, {
      capabilities: CAPABILITIES,
      chosen: [],
      onToggle() {},
      disabled: false,
      ...props,
    }),
  );

describe("the connection state machine", () => {
  test("a healthy connection reads as connected", () => {
    assert.equal(connectionState(CONNECTION()), "connected");
  });

  test("a connection whose key stopped working asks to be reconnected", () => {
    assert.equal(connectionState(CONNECTION({ status: "needs_reauth" })), "needs_reauth");
  });

  test("a connection revoked at the provider is distinguished from a broken one", () => {
    // Different causes, different remedies: one is reconnect, the other is that
    // the user already removed Neo's access at Google.
    assert.equal(connectionState(CONNECTION({ status: "revoked" })), "revoked");
  });

  test("nothing at all reads as absent rather than throwing", () => {
    assert.equal(connectionState(null), "absent");
    assert.equal(connectionState(undefined), "absent");
  });
});

describe("the provider state machine", () => {
  const provider = (overrides = {}) => ({ id: "google", connectable: true, ...overrides });

  test("a provider with no client yet asks for setup rather than reading as broken", () => {
    assert.equal(providerState(provider({ connectable: false }), []), "needs_client");
  });

  test("a configured provider with no account can be connected", () => {
    assert.equal(providerState(provider(), []), "connectable");
  });

  test("a provider that already has an account reads as linked", () => {
    assert.equal(providerState(provider(), [CONNECTION()]), "linked");
  });

  test("another provider's account does not make this one linked", () => {
    assert.equal(
      providerState(provider(), [CONNECTION({ provider: "microsoft" })]),
      "connectable",
    );
  });
});

describe("the capability picker", () => {
  test("every capability gets its own checkbox", () => {
    const markup = picker();
    assert.equal((markup.match(/type="checkbox"/g) || []).length, CAPABILITIES.length);
  });

  test("it shows the wording a person decides on, not a scope", () => {
    const markup = picker();
    assert.match(markup, /Read your calendar/);
    assert.match(markup, /See your events/);
    assert.ok(!markup.includes("googleapis.com"));
    assert.ok(!markup.includes("auth/calendar"));
  });

  test("a capability other people can see carries a warning the read-only ones do not", () => {
    const markup = picker();
    const sending = markup.slice(markup.indexOf("Send mail as you"));
    const reading = markup.slice(0, markup.indexOf("Send mail as you"));
    assert.match(sending, /Other people can see the result/);
    assert.ok(!/Other people can see the result/.test(reading));
  });

  test("only the chosen capabilities are checked", () => {
    const markup = picker({ chosen: ["mail.send"] });
    assert.equal((markup.match(/checked=""/g) || []).length, 1);
  });
});

describe("a connected account's row", () => {
  test("it names the account and says what it may do", () => {
    const markup = row({ row: CONNECTION() });
    assert.match(markup, /person@example\.com/);
    assert.match(markup, /Allowed to: calendar\.read/);
  });

  test("an account with nothing granted says so rather than showing an empty list", () => {
    assert.match(row({ row: CONNECTION({ capabilities: [] }) }), /No permissions granted/);
  });

  test("background checking is off unless the stored value says otherwise", () => {
    // The default has to be visible: this is the one switch that lets Neo reach
    // the network without being asked.
    assert.ok(!row({ row: CONNECTION() }).includes('checked=""'));
    assert.ok(row({ row: CONNECTION({ sync_enabled: true }) }).includes('checked=""'));
  });

  test("a broken connection explains the remedy", () => {
    const markup = row({ row: CONNECTION({ status: "needs_reauth" }) });
    assert.match(markup, /Reconnect needed/);
    assert.match(markup, /Disconnect and connect again/);
  });

  test("everything is disabled while a write is in flight", () => {
    const markup = row({ row: CONNECTION(), busy: true });
    assert.equal((markup.match(/disabled=""/g) || []).length, 2);
  });
});

describe("nothing sensitive is reachable", () => {
  test("a row given a credential alongside its real fields still renders none of it", () => {
    // The backend allowlist means these never arrive. This is the matching half
    // on this side: even handed one, the component has nowhere to put it.
    const markup = row({
      row: CONNECTION({
        access_token: "ya29.SECRET-ACCESS",
        refresh_token: "1//SECRET-REFRESH",
        client_secret: "SECRET-CLIENT",
      }),
    });
    for (const secret of ["ya29.SECRET-ACCESS", "1//SECRET-REFRESH", "SECRET-CLIENT"]) {
      assert.ok(!markup.includes(secret), `rendered markup leaked ${secret}`);
    }
  });

  test("the panel's own shell carries no credential either", () => {
    const markup = renderToStaticMarkup(createElement(Integrations, { onClose() {} }));
    for (const shape of ["access_token", "refresh_token", "client_secret"]) {
      assert.ok(!markup.includes(shape), `rendered markup mentioned ${shape}`);
    }
  });

  test("the shell states where the keys live rather than leaving it implied", () => {
    const markup = renderToStaticMarkup(createElement(Integrations, { onClose() {} }));
    assert.match(markup, /encrypted/i);
    assert.match(markup, /never sent anywhere else/i);
    assert.match(markup, /approval/i);
  });
});


describe("registering an OAuth client from inside the app", () => {
  const PROVIDER = (overrides = {}) => ({
    id: "google",
    display_name: "Google",
    connectable: false,
    client_mode: "none",
    client_id: "",
    redirect_uri: "http://127.0.0.1:8000/api/integrations/oauth/callback",
    capabilities: CAPABILITIES,
    ...overrides,
  });

  const setup = (props = {}) =>
    renderToStaticMarkup(
      createElement(ClientSetup, {
        provider: PROVIDER(),
        busy: false,
        onSave() {},
        onClear() {},
        ...props,
      }),
    );

  test("it shows the redirect URI to register, verbatim and selectable", () => {
    // It has to match at the provider character for character, so it is shown
    // rather than described, in a block you can select in one gesture.
    const markup = setup();
    assert.match(markup, /http:\/\/127\.0\.0\.1:8000\/api\/integrations\/oauth\/callback/);
    assert.match(markup, /integration-redirect/);
  });

  test("it asks for both halves of the client", () => {
    const markup = setup();
    assert.match(markup, /Client ID/);
    assert.match(markup, /Client secret/);
  });

  test("the secret field is masked", () => {
    assert.match(setup(), /type="password"/);
  });

  test("a stored secret is never rendered back, only reported as stored", () => {
    // The catalog reports that one is set; it never returns the value, so there
    // is nothing here to put in the field.
    const markup = setup({
      provider: PROVIDER({
        connectable: true,
        client_mode: "byo",
        client_id: "abc.apps.googleusercontent.com",
        client_secret: "SHOULD-NEVER-RENDER",
      }),
    });
    assert.ok(!markup.includes("SHOULD-NEVER-RENDER"));
    assert.match(markup, /Stored\. Type to replace\./);
  });

  test("an already-configured client offers removal, a fresh one does not", () => {
    assert.ok(!/Remove/.test(setup()));
    assert.match(
      setup({ provider: PROVIDER({ connectable: true, client_mode: "byo" }) }),
      /Remove/,
    );
  });

  test("it says where the secret goes and why it is not the thing securing sign-in", () => {
    const markup = setup();
    assert.match(markup, /encrypted/i);
    assert.match(markup, /PKCE/);
  });

  test("everything is disabled while a write is in flight", () => {
    assert.ok(setup({ busy: true }).includes('disabled=""'));
  });
});
