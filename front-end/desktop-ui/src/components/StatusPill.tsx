import type { ReactNode } from "react";
import { AlertCircle, CheckCircle2, Loader2, XCircle } from "lucide-react";
import styles from "./StatusPill.module.css";

export type StatusTone = "online" | "offline" | "loading" | "error";

export function StatusPill({ tone, children }: { tone: StatusTone; children: ReactNode }) {
  const Icon =
    tone === "online"
      ? CheckCircle2
      : tone === "error"
        ? XCircle
        : tone === "loading"
          ? Loader2
          : AlertCircle;
  return (
    <span className={[styles.pill, styles[tone]].join(" ")}>
      <Icon size={15} className={tone === "loading" ? styles.spin : ""} />
      {children}
    </span>
  );
}
