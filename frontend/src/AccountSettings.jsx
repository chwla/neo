import { useEffect, useRef, useState } from "react";

import { api } from "./api.js";
import PasswordField from "./PasswordField.jsx";

function Avatar({ profile, preview }) {
  const source = preview ?? profile?.avatar_data;
  if (source) {
    return <img className="account-avatar" src={source} alt="" />;
  }
  const initials = (profile?.username || "?").trim().slice(0, 2).toUpperCase();
  return <span className="account-avatar account-avatar-initials">{initials}</span>;
}

export default function AccountSettings({ profile, onClose, onProfileUpdated }) {
  const [username, setUsername] = useState(profile?.username || "");
  const [avatarPreview, setAvatarPreview] = useState(null);
  const [clearAvatar, setClearAvatar] = useState(false);
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const fileInput = useRef(null);

  useEffect(() => { setUsername(profile?.username || ""); }, [profile?.username]);

  const isGuest = Boolean(profile?.is_guest);
  const profileChanged =
    username.trim() !== (profile?.username || "") || avatarPreview !== null || clearAvatar;
  const passwordChanged = Boolean(currentPassword || newPassword || confirmPassword);

  function pickAvatar(event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setError(""); setNotice("");
    if (!file.type.startsWith("image/")) { setError("Choose an image file for the profile picture."); return; }
    if (file.size > 2 * 1024 * 1024) { setError("Profile pictures must be 2 MB or smaller."); return; }
    const reader = new FileReader();
    reader.onload = () => { setAvatarPreview(String(reader.result)); setClearAvatar(false); };
    reader.readAsDataURL(file);
  }

  async function saveProfile(event) {
    event.preventDefault();
    setBusy(true); setError(""); setNotice("");
    try {
      const payload = {};
      if (username.trim() !== (profile?.username || "")) payload.username = username.trim();
      if (clearAvatar) payload.clear_avatar = true;
      else if (avatarPreview) payload.avatar_data = avatarPreview;

      const data = await api.updateAccountProfile(payload);
      onProfileUpdated?.(data.profile);
      setAvatarPreview(null);
      setClearAvatar(false);
      setNotice("Profile updated.");
    } catch (requestError) {
      setError(requestError.message || "Could not update this profile.");
    } finally {
      setBusy(false);
    }
  }

  async function savePassword(event) {
    event.preventDefault();
    setError(""); setNotice("");
    if (newPassword !== confirmPassword) { setError("The new passwords do not match."); return; }
    if (newPassword.length < 4) { setError("Password must contain at least 4 characters."); return; }
    setBusy(true);
    try {
      const data = await api.updateAccountProfile({
        current_password: currentPassword,
        new_password: newPassword,
      });
      onProfileUpdated?.(data.profile);
      setCurrentPassword(""); setNewPassword(""); setConfirmPassword("");
      setNotice("Password changed. Other signed-in devices were signed out.");
    } catch (requestError) {
      setError(requestError.message || "Could not change the password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <div className="neo-dialog account-dialog" role="dialog" aria-modal="true" aria-label="Account">
        <div className="dialog-title-row">
          <h2>Account</h2>
          <button className="dialog-close" type="button" onClick={onClose} aria-label="Close">×</button>
        </div>

        {isGuest ? (
          <p className="dialog-caption">
            You are in a guest session. Guest profiles are temporary and cannot be renamed or given
            a password — create a profile to keep your work.
          </p>
        ) : (
          <>
            <p className="dialog-caption">
              Your name, picture and password are stored only on this device.
            </p>

            {error ? <div className="ws-error">{error}</div> : null}
            {notice ? <div className="ws-notice">{notice}</div> : null}

            <form className="settings-section account-section" onSubmit={saveProfile}>
              <h3>Profile</h3>
              <div className="account-identity">
                <Avatar profile={profile} preview={clearAvatar ? "" : avatarPreview} />
                <div className="account-identity-actions">
                  <input ref={fileInput} type="file" accept="image/*" hidden onChange={pickAvatar} />
                  <button type="button" onClick={() => fileInput.current?.click()} disabled={busy}>
                    Change picture
                  </button>
                  {(profile?.avatar_data || avatarPreview) && !clearAvatar ? (
                    <button
                      type="button"
                      onClick={() => { setAvatarPreview(null); setClearAvatar(true); }}
                      disabled={busy}
                    >
                      Remove
                    </button>
                  ) : null}
                  <small>PNG or JPG, up to 2 MB.</small>
                </div>
              </div>

              <label className="neo-field">
                <span className="neo-field-label">Display name</span>
                <input
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  maxLength={48}
                  autoComplete="username"
                  required
                />
              </label>

              <button className="ws-save" type="submit" disabled={busy || !profileChanged || !username.trim()}>
                {busy ? "Saving…" : "Save profile"}
              </button>
            </form>

            <form className="settings-section account-section" onSubmit={savePassword}>
              <h3>Password</h3>
              <PasswordField
                label="Current password"
                value={currentPassword}
                onChange={setCurrentPassword}
                autoComplete="current-password"
              />
              <PasswordField
                label="New password"
                value={newPassword}
                onChange={setNewPassword}
                autoComplete="new-password"
                minLength={4}
                hint="Use at least 4 characters."
              />
              <PasswordField
                label="Confirm new password"
                value={confirmPassword}
                onChange={setConfirmPassword}
                autoComplete="new-password"
              />
              <button
                className="ws-save"
                type="submit"
                disabled={busy || !passwordChanged || !currentPassword || !newPassword}
              >
                {busy ? "Saving…" : "Change password"}
              </button>
            </form>
          </>
        )}
      </div>
    </div>
  );
}
