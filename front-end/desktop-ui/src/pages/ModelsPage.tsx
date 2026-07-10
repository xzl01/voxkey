import { useCallback, useEffect, useState } from "react";
import { Cpu, MonitorCog, RefreshCw, Zap } from "lucide-react";
import { Panel } from "../components/Panel";
import { Button } from "../components/Button";
import { Toggle } from "../components/Toggle";
import { StatusPill } from "../components/StatusPill";
import { Skeleton } from "../components/Skeleton";
import { useI18n } from "../i18n";
import { useToast } from "../hooks/useToast";
import { getEngines, setEngines, LOCAL_ASR_BASE } from "../lib/api";
import { modelStatus } from "../lib/tauri";
import { useSettings } from "../hooks/useSettings";
import type { ComputeClass, EngineInfo } from "../lib/tauri";
import styles from "./ModelsPage.module.css";

const COMPUTE_ICON: Record<ComputeClass, typeof Cpu> = {
  cpu: Cpu,
  gpu: Zap,
  npu: MonitorCog,
};

function formatBytes(bytes?: number): string {
  if (!bytes || bytes <= 0) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function ModelsPage() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const { settings } = useSettings();
  const [engines, setEnginesState] = useState<EngineInfo[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [serviceReachable, setServiceReachable] = useState(true);
  const [switchingId, setSwitchingId] = useState<string | null>(null);

  // Honour the user-saved service address; fall back to the bundled default
  // only when the setting is empty.
  const baseUrl = settings.asr_service_url || LOCAL_ASR_BASE;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getEngines(baseUrl);
      setEnginesState(data);
      setServiceReachable(true);
    } catch {
      try {
        const data = await modelStatus();
        setEnginesState(data);
        setServiceReachable(false);
      } catch (err) {
        console.error("model status failed", err);
        setEnginesState(null);
      }
    } finally {
      setLoading(false);
    }
  }, [baseUrl]);

  useEffect(() => {
    load();
  }, [load]);

  const toggle = useCallback(
    async (engine: EngineInfo) => {
      setSwitchingId(engine.id);
      try {
        const next = await setEngines({ [engine.id]: !engine.enabled }, baseUrl);
        setEnginesState(next);
        // Heavy models reload asynchronously inside the service; re-confirm shortly.
        window.setTimeout(() => {
          getEngines(baseUrl)
            .then(setEnginesState)
            .catch(() => undefined);
        }, 1500);
        showToast(t("toast.modelSwitched"), "success");
      } catch (err) {
        console.error(err);
        showToast(t("toast.modelSwitchFailed"), "error");
      } finally {
        setSwitchingId(null);
      }
    },
    [t, showToast, baseUrl],
  );

  return (
    <div className={styles.page}>
      <Panel
        title={t("models.statusTitle")}
        aside={
          <span className={styles.headerAside}>
            {engines ? t("models.engines", { count: engines.length }) : ""}
            <Button variant="icon" onClick={load} aria-label={t("models.reload")}>
              <RefreshCw size={16} className={loading ? styles.spin : ""} />
            </Button>
          </span>
        }
      >
        {!serviceReachable && engines && (
          <p className={styles.banner}>{t("models.localOnly")}</p>
        )}

        {loading || !engines ? (
          <div className={styles.cardGrid}>
            {[0, 1].map((i) => (
              <div key={i} className={styles.card}>
                <Skeleton height="28px" width="60%" radius="var(--radius-sm)" />
                <Skeleton height="44px" radius="var(--radius-md)" />
              </div>
            ))}
          </div>
        ) : (
          <div className={styles.cardGrid}>
            {engines.map((engine) => {
              const Icon = COMPUTE_ICON[engine.compute];
              return (
                <article key={engine.id} className={styles.card}>
                  <div className={styles.cardHead}>
                    <span className={styles.cardIcon}>
                      <Icon size={20} />
                    </span>
                    <div className={styles.cardTitle}>
                      <strong>{engine.label}</strong>
                      <small>
                        {engine.compute.toUpperCase()} · {engine.id}
                      </small>
                    </div>
                    <Toggle
                      checked={engine.enabled}
                      loading={switchingId === engine.id}
                      disabled={!serviceReachable}
                      onChange={() => toggle(engine)}
                      label={engine.label}
                    />
                  </div>

                  <div className={styles.cardMeta}>
                    <StatusPill tone={engine.present ? "online" : "error"}>
                      {engine.present ? t("models.present") : t("models.missing")}
                    </StatusPill>
                    <StatusPill tone={engine.loaded ? "online" : "offline"}>
                      {engine.loaded ? t("models.loaded") : t("models.notLoaded")}
                    </StatusPill>
                  </div>

                  <dl className={styles.cardList}>
                    <div>
                      <dt>{t("models.path")}</dt>
                      <dd className={styles.path}>{engine.path ?? t("models.noPath")}</dd>
                    </div>
                    <div>
                      <dt>{t("models.size")}</dt>
                      <dd>{formatBytes(engine.size_bytes)}</dd>
                    </div>
                  </dl>
                </article>
              );
            })}
          </div>
        )}
      </Panel>
    </div>
  );
}
