import { Component } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { Button } from "react95";

type ErrorBoundaryProps = {
  // Identifies the crashed window in the fallback UI and in the console log.
  label: string;
  children: ReactNode;
};

type ErrorBoundaryState = {
  error: Error | null;
};

// Confines a rendering crash to the window that threw instead of unmounting
// the whole desktop. The fallback stays in the Win98 idiom.
export class ErrorBoundary extends Component<ErrorBoundaryProps, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Keep diagnostics useful without writing the raw error message or stack,
    // which may contain data returned by a panel.
    console.error(`window "${this.props.label}" crashed`, {
      errorName: error.name,
      componentStack: info.componentStack,
    });
  }

  render() {
    if (!this.state.error) {
      return this.props.children;
    }
    return (
      <div className="window-crash" role="alert">
        <h3>An error occurred in {this.props.label}.</h3>
        <p>{this.state.error.message || this.state.error.name}</p>
        <p>The rest of the desktop is unaffected. You can restart this window.</p>
        <Button type="button" onClick={() => this.setState({ error: null })}>
          Restart window
        </Button>
      </div>
    );
  }
}
