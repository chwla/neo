import { useEffect, useRef, useState } from "react";

import { API_BASE, api } from "./api.js";

function InitialsAvatar({ profile, large = false }) {
  if (profile.avatar_data) {
    return <img className={`profile-avatar ${large ? "profile-avatar-large" : ""}`} src={profile.avatar_data} alt="" />;
  }
  const initials = (profile.username || "Guest").trim().slice(0, 2).toUpperCase();
  return <span className={`profile-avatar profile-avatar-initials ${large ? "profile-avatar-large" : ""}`}>{initials}</span>;
}

export default function ProfilePicker({ onSignedIn }) {
  const [profiles, setProfiles] = useState([]);
  const [mode, setMode] = useState("choose");
  const [selected, setSelected] = useState(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [avatarData, setAvatarData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const passwordInput = useRef(null);

  async function loadProfiles() {
    try {
      const data = await api.accountProfiles();
      setProfiles(data.profiles || []);
    } catch (requestError) {
      setError(requestError.message || "Could not load profiles.");
    }
  }

  useEffect(() => { loadProfiles(); }, []);
  useEffect(() => { if (mode === "unlock") passwordInput.current?.focus(); }, [mode]);

  async function chooseGuest() {
    setBusy(true); setError("");
    try { onSignedIn((await api.createGuestProfile()).profile); }
    catch (requestError) { setError(requestError.message || "Could not start a guest session."); }
    finally { setBusy(false); }
  }

  async function unlock(event) {
    event.preventDefault(); setBusy(true); setError("");
    try { onSignedIn((await api.unlockAccountProfile(selected.id, password)).profile); }
    catch (requestError) { setPassword(""); setError(requestError.message || "Could not unlock this profile."); }
    finally { setBusy(false); }
  }

  async function create(event) {
    event.preventDefault(); setBusy(true); setError("");
    try { onSignedIn((await api.createAccountProfile({ username, password, avatar_data: avatarData })).profile); }
    catch (requestError) { setError(requestError.message || "Could not create this profile."); }
    finally { setBusy(false); }
  }

  function handleAvatar(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!file.type.startsWith("image/")) { setError("Choose an image file for the profile picture."); return; }
    if (file.size > 2 * 1024 * 1024) { setError("Profile pictures must be 2 MB or smaller."); return; }
    const reader = new FileReader();
    reader.onload = () => setAvatarData(String(reader.result));
    reader.readAsDataURL(file);
  }

  return <main className="profile-picker">
    <section className="profile-picker-card" aria-live="polite">
      <p className="profile-kicker">LOCAL PROFILES</p>
      <h1>Welcome to Neo</h1>
      <p className="profile-picker-copy">Choose a profile to open its private workspace.</p>
      {error && <p className="profile-picker-error">{error}</p>}

      {mode === "choose" && <>
        <div className="profile-grid">
          {profiles.map((profile) => <button className="profile-card" key={profile.id} onClick={() => { setSelected(profile); setPassword(""); setError(""); setMode("unlock"); }}>
            <InitialsAvatar profile={profile} large /><span>{profile.username}</span><small>Password protected</small>
          </button>)}
          <button className="profile-card profile-card-add" onClick={() => { setError(""); setMode("create"); }}>
            <span className="profile-add-icon">+</span><span>New profile</span><small>Saved on this device</small>
          </button>
          <button className="profile-card profile-card-guest" onClick={chooseGuest} disabled={busy}>
            <span className="profile-guest-icon">◌</span><span>Guest</span><small>Deleted when Neo closes</small>
          </button>
        </div>
      </>}

      {mode === "unlock" && selected && <form className="profile-form" onSubmit={unlock}>
        <button type="button" className="profile-back" onClick={() => setMode("choose")}>← All profiles</button>
        <div className="profile-unlock-heading"><InitialsAvatar profile={selected} large /><div><h2>{selected.username}</h2><p>Enter your password to continue.</p></div></div>
        <label>Password<input ref={passwordInput} type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" required /></label>
        <button className="neo-button" type="submit" disabled={busy}>{busy ? "Unlocking…" : "Unlock profile"}</button>
      </form>}

      {mode === "create" && <form className="profile-form" onSubmit={create}>
        <button type="button" className="profile-back" onClick={() => setMode("choose")}>← All profiles</button>
        <h2>Create a profile</h2><p>No email or verification needed. This profile stays only on this device.</p>
        <label className="profile-picture-input"><span>{avatarData ? <img src={avatarData} alt="Selected profile picture" className="profile-avatar profile-avatar-large" /> : "Add picture"}</span><input type="file" accept="image/*" onChange={handleAvatar} /> <em>Optional profile picture</em></label>
        <label>Username<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" maxLength="48" required /></label>
        <label>Password<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" minLength="4" required /><small>Use at least 4 characters.</small></label>
        <button className="neo-button" type="submit" disabled={busy}>{busy ? "Creating…" : "Create profile"}</button>
      </form>}
    </section>
  </main>;
}

export function GuestCleanup({ profile }) {
  useEffect(() => {
    if (!profile?.is_guest) return undefined;
    const endGuest = () => navigator.sendBeacon(`${API_BASE}/account-profiles/session/end`, "");
    window.addEventListener("pagehide", endGuest);
    return () => window.removeEventListener("pagehide", endGuest);
  }, [profile]);
  return null;
}
