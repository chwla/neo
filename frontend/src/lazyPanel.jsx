import { Component, Suspense, lazy } from "react";

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
export function lazyPanel(load) {
  const Panel = lazy(load);
  return function DeferredPanel(props) {
    return (
      <PanelErrorBoundary>
        <Suspense fallback={<p role="status" aria-live="polite">Loading…</p>}>
          <Panel {...props} />
        </Suspense>
      </PanelErrorBoundary>
    );
  };
}
