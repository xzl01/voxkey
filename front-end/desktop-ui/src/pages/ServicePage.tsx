import { useCallback, useEffect, useState } from "react";
import { Download, Play, RefreshCw, Square } from "lucide-react";
import { Button } from "../components/Button";
import { Panel } from "../components/Panel";
import { StatusPill } from "../components/StatusPill";
import { useI18n } from "../i18n";
import { useSettings } from "../hooks/useSettings";
import { useServiceStatus } from "../hooks/useServiceStatus";
import { useToast } from "../hooks/useToast";
import { getHealth, type HealthResponse } from "../lib/api";
import { startAsrService, stopAsrService, startModelDownload, modelStatus, type EngineInfo } from "../lib/tauri";
import styles from "./ServicePage.module.css";

export function ServicePage() {
  const { t } = useI18n();
  const { settings } = useSettings();
  const { showToast } = useToast();
  const isRemote = settings.asr_backend === "http";
  const local = useServiceStatus(!isRemote);
  const [health, setHealth] = useState<HealthResponse | null>(null);
  const [models, setModels] = useState<EngineInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [downloading, setDownloading] = useState(false);

  // True when at least one configured engine has no model weights present yet
  // (the weights are fetched from the release on first launch, not bundled).
  const modelsMissing = models.some((m) => !m.present);

  const refreshAll = useCallback(async () => {
    if (isRemote) return;
    await local.refresh();
    try {
      setHealth(await getHealth(settings.asr_service_url));
    } catch {
      setHealth(null);
    }
    try {
      setModels(await modelStatus());
    } catch {
      setModels([]);
    }
  }, [isRemote, local, settings.asr_service_url]);

  useEffect(() => {
    if (!isRemote) refreshAll();
  }, [isRemote, refreshAll]);

  const handleStart = async () => {
    setBusy(true);
    try {
      await startAsrService();
    } catch (err) {
      console.error(err);
      showToast(t("toast.serviceStartFailed"), "error");
    } finally {
      setBusy(false);
      await refreshAll();
    }
  };

  const handleStop = async () => {
    setBusy(true);
    try {
      await stopAsrService();
    } catch (err) {
      console.error(err);
      showToast(t("toast.serviceStopFailed"), "error");
    } finally {
      setBusy(false);
      await refreshAll();
    }
  };

  const handleDownload = async () => {
    setDownloading(true);
    try {
      await startModelDownload();
      showToast(t("toast.modelDownloadStarted"), "info");
    } catch (err) {
      console.error(err);
      showToast(t("toast.modelDownloadFailed"), "error");
    } finally {
      setDownloading(false);
      // Poll for a short while so the UI reflects progress without a manual refresh.
      let ticks = 0;
      const timer = setInterval(async () => {
        await refreshAll();
        if (++ticks >= 20) {
          clearInterval(timer);
        }
      }, 4000);
    }
  };

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
            <div className={styles.controls}>
              <Button
                variant="primary"
                onClick={handleStart}
                disabled={busy || local.status?.reachable}
              >
                <Play size={16} /> {busy && !local.status?.reachable ? t("service.starting") : t("service.start")}
              </Button>
              <Button
                variant="ghost"
                onClick={handleStop}
                disabled={busy || !local.status?.reachable}
              >
                <Square size={16} /> {t("service.stop")}
              </Button>
              {modelsMissing && (
                <Button
                  variant="subtle"
                  onClick={handleDownload}
                  disabled={downloading}
                >
                  <Download size={16} /> {downloading ? t("service.downloading") : t("service.downloadModels")}
                </Button>
              )}
            </div>

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
