/**
 * Origin handling for the Vite dev proxy.
 *
 * The backend rejects a write whose Origin is not its own (OriginGuardMiddleware).
 * Through the dev proxy the page's origin is the dev server's, so every POST was
 * refused unless NEO_DEV_CORS_ENABLED was set -- which no setup step said. The
 * proxy rewrites Origin to the backend's, but only when it is exactly the dev
 * server's own origin: a request from any other page keeps its Origin, and the
 * backend goes on refusing it. The CSRF double-submit check still applies.
 */
export function rewriteDevOrigin(origin, devServerHost, target) {
  if (!origin || !devServerHost) return origin;
  return origin === `http://${devServerHost}` ? new URL(target).origin : origin;
}
