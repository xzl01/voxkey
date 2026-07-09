import { Languages, Moon, Sun } from "lucide-react";
import { useI18n } from "../i18n";
import { useTheme } from "../theme/ThemeProvider";
import styles from "./TopBar.module.css";

interface TopBarProps {
  title: string;
  subtitle?: string;
}

export function TopBar({ title, subtitle }: TopBarProps) {
  const { theme, toggleTheme } = useTheme();
  const { lang, toggleLang, t } = useI18n();

  return (
    <header className={styles.topbar}>
      <div className={styles.heading}>
        <h1 className={styles.title}>{title}</h1>
        {subtitle && <p className={styles.subtitle}>{subtitle}</p>}
      </div>
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.iconBtn}
          onClick={toggleTheme}
          aria-label={t("topbar.toggleTheme")}
          title={t("topbar.toggleTheme")}
        >
          {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
        </button>
        <button
          type="button"
          className={styles.langBtn}
          onClick={toggleLang}
          aria-label={t("topbar.toggleLang")}
          title={t("topbar.toggleLang")}
        >
          <Languages size={16} />
          <span className={styles.langLabel}>{lang === "zh" ? "中" : "EN"}</span>
        </button>
      </div>
    </header>
  );
}
