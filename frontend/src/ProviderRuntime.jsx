import { useEffect, useState } from "react";

import { api } from "./api.js";

export default function ProviderRuntime() {
  const [data, setData] = useState({});
  const [error, setError] = useState("");
  const load = async () => {
    try {
      const [status, health, requests, usage, limits] = await Promise.all([
        api.providerRuntimeStatus(), api.providerRuntimeHealth(), api.providerRuntimeRequests(),
        api.providerRuntimeUsage(), api.providerRuntimeRateLimits(),
      ]);
      setData({ status, health, requests: requests.requests || [], usage, limits: limits.rate_limits || [] }); setError("");
    } catch (err) { setError(err.message || "Provider Runtime is unavailable."); }
  };
  useEffect(() => { load(); }, []);
  return <section className="agentic-workspace"><header className="agentic-header"><div><p className="settings-kicker">Reliable routing · retries · redacted audit</p><h2>Provider Runtime</h2></div><button type="button" onClick={load}>Refresh</button></header>{error && <p className="settings-error">{error}</p>}<div className="agentic-detail-grid"><section className="settings-section"><h3>Routes</h3>{(data.status?.routes || []).map((item) => <p key={item.route_name}><strong>{item.route_name}</strong> · {item.provider || item.status} / {item.model || "unavailable"}</p>)}</section><section className="settings-section"><h3>Usage</h3><pre>{JSON.stringify(data.usage || {}, null, 2)}</pre><h3>Rate limits</h3><pre>{JSON.stringify(data.limits || [], null, 2)}</pre></section></div><section className="settings-section"><h3>Provider health</h3><pre>{JSON.stringify(data.health?.checks || [], null, 2)}</pre></section><section className="settings-section"><h3>Recent requests</h3>{data.requests?.length ? data.requests.map((item) => <details key={item.id}><summary>{item.status} · {item.route_name} · {item.provider_name}/{item.model_name}</summary><pre>{JSON.stringify(item, null, 2)}</pre></details>) : <p>No provider requests yet.</p>}</section></section>;
}
