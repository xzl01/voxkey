import type { ReactNode } from "react";
import styles from "./Toggle.module.css";

interface ToggleProps {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  loading?: boolean;
  label?: ReactNode;
}

export function Toggle({ checked, onChange, disabled, loading, label }: ToggleProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={typeof label === "string" ? label : undefined}
      aria-busy={loading || undefined}
      className={[styles.track, checked ? styles.on : "", disabled ? styles.disabled : ""]
        .filter(Boolean)
        .join(" ")}
      disabled={disabled || loading}
      onClick={() => onChange(!checked)}
    >
      <span className={styles.thumb} />
    </button>
  );
}
