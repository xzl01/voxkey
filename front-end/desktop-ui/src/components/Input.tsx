import { useId, type InputHTMLAttributes, type ReactNode } from "react";
import styles from "./Input.module.css";

interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  label?: ReactNode;
  hint?: ReactNode;
  mono?: boolean;
}

export function Input({ label, hint, mono, className, id, ...rest }: InputProps) {
  const autoId = useId();
  const inputId = id ?? autoId;
  return (
    <label className={styles.field} htmlFor={inputId}>
      {label && <span className={styles.label}>{label}</span>}
      <input
        id={inputId}
        className={[styles.input, mono ? styles.mono : "", className ?? ""].filter(Boolean).join(" ")}
        {...rest}
      />
      {hint && <span className={styles.hint}>{hint}</span>}
    </label>
  );
}
