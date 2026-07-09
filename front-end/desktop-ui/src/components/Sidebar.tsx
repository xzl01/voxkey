import type { ReactNode } from "react";
import { HardDriveDownload, Mic, MonitorCog, RadioTower } from "lucide-react";
import { useI18n } from "../i18n";
import styles from "./Sidebar.module.css";

export type PageId = "setup" | "models" | "capture" | "service";

const NAV: { id: PageId; labelKey: "nav.setup" | "nav.models" | "nav.capture" | "nav.service"; icon: ReactNode }[] =
  [
    { id: "setup", labelKey: "nav.setup", icon: <MonitorCog size={18} /> },
    { id: "models", labelKey: "nav.models", icon: <HardDriveDownload size={18} /> },
    { id: "capture", labelKey: "nav.capture", icon: <Mic size={18} /> },
    { id: "service", labelKey: "nav.service", icon: <RadioTower size={18} /> },
  ];

export function Sidebar({
  active,
  onNavigate,
}: {
  active: PageId;
  onNavigate: (id: PageId) => void;
}) {
  const { t } = useI18n();
  return (
    <aside className={styles.sidebar}>
      <div className={styles.brand}>
        <div className={styles.mark}>简</div>
        <div className={styles.brandText}>
          <div className={styles.brandTitle}>简听输入</div>
          <div className={styles.brandSubtitle}>{t("app.subtitle")}</div>
        </div>
      </div>
      <nav className={styles.nav} aria-label="Primary">
        {NAV.map((item) => (
          <button
            key={item.id}
            type="button"
            className={[styles.item, active === item.id ? styles.active : ""].filter(Boolean).join(" ")}
            aria-current={active === item.id ? "page" : undefined}
            onClick={() => onNavigate(item.id)}
          >
            {item.icon}
            <span>{t(item.labelKey)}</span>
          </button>
        ))}
      </nav>
    </aside>
  );
}
