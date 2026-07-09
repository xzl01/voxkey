import type { ReactNode } from "react";
import styles from "./Panel.module.css";

interface PanelProps {
  title?: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}

export function Panel({ title, aside, children, className }: PanelProps) {
  return (
    <section className={[styles.panel, className ?? ""].filter(Boolean).join(" ")}>
      {(title || aside) && (
        <header className={styles.header}>
          {title && <h2 className={styles.title}>{title}</h2>}
          {aside && <div className={styles.aside}>{aside}</div>}
        </header>
      )}
      <div className={styles.body}>{children}</div>
    </section>
  );
}
