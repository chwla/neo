// The N, E and O sit on the three visible faces of an isometric cube.
export default function NeoLogo() {
  return (
    <svg className="neo-logo-mark" viewBox="-110 -110 220 220" aria-hidden="true" focusable="false">
      <path
        transform="matrix(0.86603 0.5 0 1 -86.603 -50)"
        fill="var(--neo-fg-1)"
        d="M5 8H25L75 68V8H95V92H75L25 32V92H5Z"
      />
      <path
        transform="matrix(0.86603 -0.5 0.86603 0.5 -86.603 -50)"
        fill="var(--neo-accent)"
        d="M5 8H95V26H25V41H85V59H25V74H95V92H5Z"
      />
      <path
        transform="matrix(0.86603 -0.5 0 1 0 0)"
        fill="var(--neo-accent)"
        fillRule="evenodd"
        d="M17 8H83A12 12 0 0 1 95 20V80A12 12 0 0 1 83 92H17A12 12 0 0 1 5 80V20A12 12 0 0 1 17 8ZM33 31H67A5 5 0 0 1 72 36V64A5 5 0 0 1 67 69H33A5 5 0 0 1 28 64V36A5 5 0 0 1 33 31Z"
      />
    </svg>
  );
}
