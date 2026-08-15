import { useId, useState } from "react";

const EYE = ["M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z", "M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6z"];
const EYE_OFF = ["M4 4l16 16", "M9.9 5.2A9.9 9.9 0 0 1 12 5c6 0 10 7 10 7a17 17 0 0 1-3.2 3.9", "M6.2 8.1A17 17 0 0 0 2 12s4 7 10 7a9.7 9.7 0 0 0 4-.85"];

/**
 * Password input with a reveal toggle. The toggle is a button rather than a
 * checkbox so it never submits the surrounding form.
 */
export default function PasswordField({
  label,
  value,
  onChange,
  autoComplete = "current-password",
  hint,
  minLength,
  required = false,
  disabled = false,
  inputRef,
  id,
}) {
  const [visible, setVisible] = useState(false);
  const generatedId = useId();
  const fieldId = id || generatedId;

  return (
    <label className="password-field" htmlFor={fieldId}>
      {label ? <span className="password-field-label">{label}</span> : null}
      <span className="password-field-control">
        <input
          id={fieldId}
          ref={inputRef}
          type={visible ? "text" : "password"}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          autoComplete={autoComplete}
          minLength={minLength}
          required={required}
          disabled={disabled}
        />
        <button
          type="button"
          className="password-reveal"
          onClick={() => setVisible((shown) => !shown)}
          aria-label={visible ? "Hide password" : "Show password"}
          aria-pressed={visible}
          title={visible ? "Hide password" : "Show password"}
          tabIndex={-1}
        >
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            {(visible ? EYE_OFF : EYE).map((path) => <path d={path} key={path} />)}
          </svg>
        </button>
      </span>
      {hint ? <small className="password-field-hint">{hint}</small> : null}
    </label>
  );
}
