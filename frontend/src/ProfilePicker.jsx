import { useEffect, useRef, useState } from "react";

import { api } from "./api.js";
import ChatBackground from "./ChatBackground.jsx";
import { DEFAULT_BACKGROUND_ID, DEFAULT_INTENSITY_ID } from "./backgrounds/index.js";
import NeoLogo from "./NeoLogo.jsx";
import PasswordField from "./PasswordField.jsx";

function InitialsAvatar({ profile, large = false }) {
  if (profile.avatar_data) {
    return <img className={`profile-avatar ${large ? "profile-avatar-large" : ""}`} src={profile.avatar_data} alt="" />;
  }
  const initials = (profile.username || "Guest").trim().slice(0, 2).toUpperCase();
  return <span className={`profile-avatar profile-avatar-initials ${large ? "profile-avatar-large" : ""}`}>{initials}</span>;
}

export default function ProfilePicker({ onSignedIn }) {
  const [profiles, setProfiles] = useState([]);
  const [loadingProfiles, setLoadingProfiles] = useState(true);
  const [mode, setMode] = useState("choose");
  const [selected, setSelected] = useState(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [avatarData, setAvatarData] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const passwordInput = useRef(null);

  async function loadProfiles() {
    setLoadingProfiles(true);
    try {
      const data = await api.accountProfiles();
      setProfiles(data.profiles || []);
    } catch (requestError) {
      setError(requestError.message || "Could not load profiles.");
    } finally { setLoadingProfiles(false); }
  }

  useEffect(() => { loadProfiles(); }, []);
  useEffect(() => { if (mode === "unlock" || mode === "delete") passwordInput.current?.focus(); }, [mode]);

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

  async function deleteAccount(event) {
    event.preventDefault();
    if (!selected) return;
    setBusy(true); setError("");
    try {
      await api.deleteAccountProfile(selected.id, password);
      setPassword(""); setSelected(null); setMode("choose");
      await loadProfiles();
    } catch (requestError) {
      setPassword("");
      setError(requestError.message || "Could not delete this profile.");
    } finally { setBusy(false); }
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

  // No profile is known yet, so there is no stored choice to honour: the
  // sign-in screens always wear the default background.
  return <main className={`profile-picker profile-picker-${mode}`}>
    <ChatBackground background={DEFAULT_BACKGROUND_ID} intensity={DEFAULT_INTENSITY_ID} />
    <section className={`profile-shell profile-shell-${mode}`}>
      <div className="profile-brand" aria-label="Neo"><NeoLogo /><span>neo</span></div>

      {mode !== "choose" && <button type="button" className="profile-back" onClick={() => {
        setPassword(""); setError(""); setMode(mode === "delete" ? "unlock" : "choose");
      }}><span aria-hidden="true">←</span>{mode === "delete" ? "Keep profile" : "All profiles"}</button>}

      {mode === "choose" && <>
        <header className="profile-intro">
          <h1>Choose a profile</h1>
          <p>Select a local workspace to continue.</p>
        </header>
        {error && <p className="profile-picker-error" role="alert">{error}</p>}
        {loadingProfiles ? <p className="profile-empty" role="status">Loading profiles…</p> : profiles.length > 0 ? <div className="profile-grid">
          {profiles.map((profile) => <button type="button" className="profile-option" key={profile.id} disabled={busy} onClick={() => {
            setSelected(profile); setPassword(""); setError(""); setMode("unlock");
          }} aria-label={`Open ${profile.username} profile`}>
            <InitialsAvatar profile={profile} />
            <span className="profile-option-name">{profile.username}</span>
            <span className="profile-option-arrow" aria-hidden="true">→</span>
          </button>)}
        </div> : !error && <p className="profile-empty">No profiles on this device yet. Create one to get started.</p>}
        <div className="profile-actions">
          <button type="button" className="profile-create-action" disabled={busy} onClick={() => { setError(""); setMode("create"); }}>
            <span className="profile-create-icon" aria-hidden="true">+</span>Create profile
          </button>
          <div className="profile-guest-action">
            <button type="button" onClick={chooseGuest} disabled={busy} aria-describedby="profile-guest-note">Continue as guest <span aria-hidden="true">→</span></button>
            <small id="profile-guest-note">Cleared when Neo closes</small>
          </div>
        </div>
      </>}

      {mode === "unlock" && selected && <>
        <div className="profile-identity">
          <InitialsAvatar profile={selected} large />
          <h1>{selected.username}</h1>
          <p>Enter your password to continue.</p>
        </div>
        {error && <p className="profile-picker-error" role="alert">{error}</p>}
        <form className="profile-form" onSubmit={unlock}>
          <PasswordField label="Password" inputRef={passwordInput} value={password} onChange={setPassword} autoComplete="current-password" required />
          <button className="profile-submit" type="submit" disabled={busy}>{busy ? "Unlocking…" : "Unlock profile"}<span aria-hidden="true">→</span></button>
        </form>
        <details className="profile-options">
          <summary>Profile options <span aria-hidden="true">⌄</span></summary>
          <button type="button" onClick={() => { setPassword(""); setError(""); setMode("delete"); }} disabled={busy}>Delete this profile</button>
        </details>
      </>}

      {mode === "delete" && selected && <>
        <div className="profile-identity profile-delete-intro">
          <InitialsAvatar profile={selected} large />
          <h1>Delete {selected.username}?</h1>
          <p>This permanently removes its chats, memories, files, settings, and local credentials from this device.</p>
        </div>
        {error && <p className="profile-picker-error" role="alert">{error}</p>}
        <form className="profile-form" onSubmit={deleteAccount}>
          <PasswordField label="Enter password to confirm" inputRef={passwordInput} value={password} onChange={setPassword} autoComplete="current-password" required />
          <button className="profile-submit profile-danger-button" type="submit" disabled={busy}>{busy ? "Deleting…" : "Delete profile permanently"}</button>
        </form>
      </>}

      {mode === "create" && <>
        <header className="profile-intro profile-form-intro">
          <h1>Create a profile</h1>
          <p>Your private workspace stays on this device.</p>
        </header>
        {error && <p className="profile-picker-error" role="alert">{error}</p>}
        <form className="profile-form" onSubmit={create}>
          <label className="profile-picture-input">
            <span className="profile-picture-preview">{avatarData ? <img src={avatarData} alt="" className="profile-avatar profile-avatar-large" /> : <span aria-hidden="true">+</span>}</span>
            <span className="profile-picture-copy">{avatarData ? "Change photo" : "Add a photo"}<small>Optional</small></span>
            <input type="file" accept="image/*" onChange={handleAvatar} aria-label="Profile picture" />
          </label>
          <label>Username<input value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" maxLength={48} required /></label>
          <PasswordField label="Password" value={password} onChange={setPassword} autoComplete="new-password" required />
          <button className="profile-submit" type="submit" disabled={busy}>{busy ? "Creating…" : "Create profile"}<span aria-hidden="true">→</span></button>
        </form>
      </>}
    </section>
  </main>;
}
