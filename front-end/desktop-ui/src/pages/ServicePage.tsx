import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { Button } from "../components/Button";
import { Panel } from "../components/Panel";
import { StatusPill } from "../components/StatusPill";
import { useI18n } from "../i18n";
import { useSettings } from "../hooks/useSettings";
import { useServiceStatus } from "../hooks/useServiceStatus";
import { getHealth, type HealthResponse } from "../lib/api";
import styles from "./ServicePage.module.css";

export function ServicePage() {
  const { t } = useI18n();
  const { settings } = useSettings();
  const isRemote = settings.asr_backend === "http";
  const local = useServiceStatus(!isRemote);
  const [health, setHealth] = useState<HealthResponse | null>(null);

  const refreshAll = useCallback(async () => {
    if (isRemote) return;
    await local.refresh();
    try {
      setHealth(await getHealth(settings.asr_service_url));
    } catch {
      setHealth(null);
    }
  }, [isRemote, local, settings.asr_service_url]);

  useEffect(() => {
    if (!isRemote) refreshAll();
  }, [isRemote, refreshAll]);

  const statusTone = local.state === "loading" ? "loading" : local.status?.reachable ? "online" : "error";

  return (
    <div className={styles.page}>
      <Panel
        title={t("service.title")}
        aside={
          <Button variant="icon" onClick={refreshAll} aria-label={t("service.refresh")}>
            <RefreshCw size={16} className={local.state === "loading" ? styles.spin : ""} />
          </Button>
        }
      >
        {isRemote ? (
          <div className={styles.remoteBlock}>
            <dl className={styles.infoList}>
              <div>
                <dt>{t("service.address")}</dt>
                <dd className={styles.mono}>{settings.asr_service_url}</dd>
              </div>
              <div>
                <dt>{t("service.maskedKey")}</dt>
                <dd className={styles.mono}>{settings.asr_api_key ? "••••••••••••" : t("models.noPath")}</dd>
              </div>
            </dl>
            <p className={styles.note}>{t("service.remoteNote")}</p>
          </div>
        ) : (
          <>
            <dl className={styles.infoList}>
              <div>
                <dt>{t("service.address")}</dt>
                <dd className={styles.mono}>{local.status?.url ?? settings.asr_service_url}</dd>
              </div>
              <div>
                <dt>{t("service.status")}</dt>
                <dd>
                  <StatusPill tone={statusTone}>
                    {local.state === "loading"
                      ? t("service.checking")
                      : local.status?.status ?? t("common.offline")}
                  </StatusPill>
                </dd>
              </div>
              {local.status?.detail && (
                <div>
                  <dt>{t("service.detail")}</dt>
                  <dd className={styles.detail}>{local.status.detail}</dd>
                </div>
              )}
            </dl>

            <h3 className={styles.enginesTitle}>{t("service.enginesTitle")}</h3>
            {health?.engines?.length ? (
              <table className={styles.table}>
                <thead>
                  <tr>
                    <th>{t("service.engineKind")}</th>
                    <th>{t("service.engineCompute")}</th>
                  </tr>
                </thead>
                <tbody>
                  {health.engines.map((engine) => (
                    <tr key={engine.kind}>
                      <td className={styles.mono}>{engine.kind}</td>
                      <td>{engine.compute}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            ) : (
              <p className={styles.note}>
                {local.status?.reachable ? t("service.noHealth") : t("common.offline")}
              </p>
            )}
          </>
        )}
      </Panel>
    </div>
  );
}
