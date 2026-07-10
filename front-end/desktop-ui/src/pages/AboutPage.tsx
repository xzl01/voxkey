import { useEffect, useState } from "react";
import { Panel } from "../components/Panel";
import { useI18n } from "../i18n";
import { getLicenses } from "../lib/tauri";
import styles from "./ServicePage.module.css";

export function AboutPage() {
  const { t } = useI18n();
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    getLicenses()
      .then((v) => active && setText(v))
      .catch((err) => active && setError(String(err)));
    return () => {
      active = false;
    };
  }, []);

  return (
    <div className={styles.page}>
      <Panel title={t("about.title")}>
        <p className={styles.note}>{t("about.intro")}</p>
        {error && <p className={styles.detail}>{error}</p>}
        {text === null && !error && <p className={styles.note}>{t("about.loading")}</p>}
        {text !== null && (
          <pre className={styles.licenseText}>{text}</pre>
        )}
      </Panel>
    </div>
  );
}
