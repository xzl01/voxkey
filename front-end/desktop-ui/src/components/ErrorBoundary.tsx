import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("UI render error:", error, info);
  }

  private handleRetry = () => this.setState({ error: null });

  render() {
    if (this.state.error) {
      return <ErrorFallback message={this.state.error.message} onRetry={this.handleRetry} />;
    }
    return this.props.children;
  }
}

function ErrorFallback({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      style={{
        height: "100%",
        display: "grid",
        placeItems: "center",
        padding: "var(--space-8)",
        textAlign: "center",
      }}
    >
      <div
        style={{
          maxWidth: 420,
          background: "var(--bg-surface)",
          border: "1px solid var(--border-default)",
          borderRadius: "var(--radius-lg)",
          padding: "var(--space-8)",
          boxShadow: "var(--shadow-md)",
        }}
      >
        <h2 style={{ marginBottom: "var(--space-3)" }}>出错了</h2>
        <p style={{ color: "var(--text-muted)", marginBottom: "var(--space-5)" }}>{message}</p>
        <button
          type="button"
          onClick={onRetry}
          style={{
            border: 0,
            borderRadius: "var(--radius-sm)",
            background: "var(--color-primary)",
            color: "var(--color-primary-contrast)",
            padding: "var(--space-3) var(--space-5)",
            cursor: "pointer",
          }}
        >
          重试
        </button>
      </div>
    </div>
  );
}
