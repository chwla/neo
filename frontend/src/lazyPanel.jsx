import { Component, Suspense, lazy, useState } from "react";

class PanelErrorBoundary extends Component {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (this.state.failed) {
      return <p role="alert">This view could not load. Reload Neo to try again.</p>;
    }
    return this.props.children;
  }
}

// Each view owns its loading boundary so opening a panel never hides the chat
// or discards an unsent draft while its code downloads.
//
// `preload()` starts the same download early (see panelPreload.js) and shares
// it with the first real open. A failed preload is forgotten, so the real open
// tries again rather than inheriting a failure from the background. Each
// mounted panel picks its component once: one already downloaded renders at
// once, with no loading flash; one that is not keeps the fallback above. An
// open panel never swaps component type under its own state.
export function lazyPanel(load) {
  let resolved = null;
  let inflight = null;
  const preload = () => {
    if (resolved) return Promise.resolve(resolved);
    if (!inflight) {
      inflight = load().then(
        (module) => {
          resolved = module.default;
          return resolved;
        },
        (error) => {
          inflight = null;
          throw error;
        },
      );
    }
    return inflight;
  };
  const LazyPanel = lazy(() => preload().then((component) => ({ default: component })));
  function DeferredPanel(props) {
    const [Panel] = useState(() => resolved ?? LazyPanel);
    return (
      <PanelErrorBoundary>
        <Suspense fallback={<p role="status" aria-live="polite">Loading…</p>}>
          <Panel {...props} />
        </Suspense>
      </PanelErrorBoundary>
    );
  }
  DeferredPanel.preload = preload;
  DeferredPanel.isLoaded = () => resolved !== null;
  return DeferredPanel;
}
