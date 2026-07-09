import { AlertCircle, CheckCircle2, Info, X } from "lucide-react";
import type { ToastItem } from "../hooks/useToast";
import styles from "./Toast.module.css";

export function ToastViewport({ toasts }: { toasts: ToastItem[] }) {
  return (
    <div className={styles.viewport} role="region" aria-live="polite" aria-label="Notifications">
      {toasts.map((toast) => {
        const Icon = toast.tone === "success" ? CheckCircle2 : toast.tone === "error" ? AlertCircle : Info;
        return (
          <div key={toast.id} className={`${styles.toast} ${styles[toast.tone]}`}>
            <Icon size={18} className={styles.icon} />
            <span className={styles.message}>{toast.message}</span>
            <span className={styles.glyph} aria-hidden>
              <X size={14} />
            </span>
          </div>
        );
      })}
    </div>
  );
}
