import { Cpu, MonitorCog, RadioTower, RefreshCw, Save, Zap } from "lucide-react";
import type { PageId } from "../components/Sidebar";
import { Button } from "../components/Button";
import { Input } from "../components/Input";
import { Panel } from "../components/Panel";
import { Segmented } from "../components/Segmented";
import { StatusPill } from "../components/StatusPill";
import { useI18n } from "../i18n";
import { useServiceStatus } from "../hooks/useServiceStatus";
import { useSettings } from "../hooks/useSettings";
import type { ComputeClass } from "../lib/tauri";
import styles from "./SetupPage.module.css";

const COMPUTE_ICON: Record<ComputeClass, typeof Cpu> = {
  cpu: Cpu,
  gpu: Zap,
  npu: MonitorCog,
};

export function SetupPage({ onNavigate }: { onNavigate?: (id: PageId) => void }) {
  const { t } = useI18n();
  const s = useSettings();
  const isRemote = s.settings.asr_backend === "http";
  const service = useServiceStatus(!isRemote);

  void onNavigate;

  return (
    <div className={styles.page}>
      <div className={styles.grid}>
        <Panel title={t("setup.computePlan")} aside={t("setup.options", { count: s.candidates.length })}>
          <ul className={styles.optionList}>
            {s.candidates.map((candidate) => {
              const Icon = COMPUTE_ICON[candidate.compute];
              const selected = s.selectedCandidate?.id === candidate.id;
              return (
                <li key={candidate.id}>
                  <button
                    type="button"
                    className={[styles.option, selected ? styles.optionSelected : ""].filter(Boolean).join(" ")}
                    aria-pressed={selected}
                    onClick={() => s.update({ selected_runtime_id: candidate.id })}
                  >
                    <Icon size={22} className={styles.optionIcon} />
                    <span className={styles.optionText}>
                      <strong>{candidate.label}</strong>
                      <small>{candidate.runtime}</small>
                    </span>
                    {candidate.recommended && <em className={styles.recommended}>{t("setup.recommended")}</em>}
                  </button>
                </li>
              );
            })}
          </ul>
        </Panel>

        <Panel title={t("setup.selectedBackend")} aside={s.selectedCandidate?.platform}>
          {s.selectedCandidate ? (
            <dl className={styles.detailList}>
              <div>
                <dt>{t("setup.field.class")}</dt>
                <dd>{s.selectedCandidate.compute.toUpperCase()}</dd>
              </div>
              <div>
                <dt>{t("setup.field.runtime")}</dt>
                <dd>{s.selectedCandidate.runtime}</dd>
              </div>
              <div>
                <dt>{t("setup.field.status")}</dt>
                <dd>
                  {s.selectedCandidate.installed ? t("setup.installed") : t("setup.notInstalled")}
                </dd>
              </div>
              <div>
                <dt>{t("setup.field.saved")}</dt>
                <dd>
                  {s.settings.selected_runtime_id === s.savedSettings.selected_runtime_id
                    ? t("setup.currentPref")
                    : t("setup.notSaved")}
                </dd>
              </div>
              <div>
                <dt>{t("setup.field.notes")}</dt>
                <dd>{s.selectedCandidate.notes}</dd>
              </div>
            </dl>
          ) : (
            <p className={styles.muted}>{t("common.loading")}</p>
          )}
          <Button
            fullWidth
            disabled={!s.hasUnsavedRuntime || s.runtimeSave === "saving" || !s.selectedCandidate}
            onClick={() => s.selectedCandidate && s.saveRuntime(s.selectedCandidate.id)}
          >
            <Save size={16} />
            {s.runtimeSave === "saving"
              ? t("setup.saving")
              : s.hasUnsavedRuntime
                ? t("setup.saveSelection")
                : t("setup.saved")}
          </Button>
          {s.runtimeSave === "error" && <p className={styles.inlineError}>{t("setup.saveFailed")}</p>}
        </Panel>
      </div>

      <Panel
        title={t("setup.asrBackend")}
        aside={isRemote ? t("setup.backend.remote") : t("setup.backend.local")}
      >
        <Segmented
          value={s.settings.asr_backend}
          ariaLabel={t("setup.asrBackend")}
          onChange={(value) => s.update({ asr_backend: value })}
          options={[
            {
              value: "local",
              label: t("setup.backend.local"),
              desc: t("setup.backend.localDesc"),
              icon: <Cpu size={20} />,
            },
            {
              value: "http",
              label: t("setup.backend.remote"),
              desc: t("setup.backend.remoteDesc"),
              icon: <RadioTower size={20} />,
            },
          ]}
        />

        <div className={styles.backendFields}>
          {isRemote ? (
            <>
              <Input
                label={t("setup.serviceUrl")}
                placeholder={t("setup.serviceUrlPlaceholder")}
                mono
                value={s.settings.asr_service_url}
                onChange={(e) => s.update({ asr_service_url: e.target.value })}
              />
              <Input
                label={t("setup.apiKey")}
                type="password"
                placeholder={t("setup.apiKeyPlaceholder")}
                mono
                value={s.settings.asr_api_key}
                onChange={(e) => s.update({ asr_api_key: e.target.value })}
              />
              <Input
                label={t("setup.remoteModel")}
                placeholder={t("setup.remoteModelPlaceholder")}
                value={s.settings.asr_remote_model}
                onChange={(e) => s.update({ asr_remote_model: e.target.value })}
              />
            </>
          ) : (
            <>
              <Input
                label={t("setup.serviceUrl")}
                placeholder={t("setup.serviceUrlPlaceholder")}
                mono
                value={s.settings.asr_service_url}
                onChange={(e) => s.update({ asr_service_url: e.target.value })}
              />
              <label className={styles.checkRow}>
                <input
                  type="checkbox"
                  checked={s.settings.asr_fallback_local}
                  onChange={(e) => s.update({ asr_fallback_local: e.target.checked })}
                />
                <span>{t("setup.fallback")}</span>
              </label>
              <Input
                label={`${t("setup.timeout")} (${t("setup.timeoutUnit")})`}
                type="number"
                min={1}
                value={s.settings.asr_http_timeout}
                onChange={(e) => s.update({ asr_http_timeout: Number(e.target.value) || 0 })}
              />
            </>
          )}
        </div>

        <Button
          fullWidth
          disabled={!s.hasUnsavedBackend || s.backendSave === "saving"}
          onClick={s.saveBackend}
        >
          <Save size={16} />
          {s.backendSave === "saving"
            ? t("setup.saving")
            : s.hasUnsavedBackend
              ? t("setup.saveBackend")
              : t("setup.saved")}
        </Button>
        {s.backendSave === "error" && <p className={styles.inlineError}>{t("setup.saveFailed")}</p>}
      </Panel>

      <section className={styles.serviceStrip} aria-label={t("setup.serviceTitle")}>
        {isRemote ? (
          <>
            <div>
              <h2 className={styles.stripTitle}>{t("setup.serviceTitle")}</h2>
              <p className={styles.stripUrl}>{s.settings.asr_service_url}</p>
            </div>
            <div className={styles.stripState}>
              <StatusPill tone="offline">{t("service.remoteTitle")}</StatusPill>
              <small>{t("service.remoteNote")}</small>
            </div>
            <div className={styles.stripKey}>
              <span>{t("service.maskedKey")}</span>
              <strong>{s.settings.asr_api_key ? "••••••••••••" : t("models.noPath")}</strong>
            </div>
          </>
        ) : (
          <>
            <div>
              <h2 className={styles.stripTitle}>{t("setup.serviceTitle")}</h2>
              <p className={styles.stripUrl}>
                {service.status?.url ?? s.settings.asr_service_url}
              </p>
            </div>
            <div className={styles.stripState}>
              <span
                className={[
                  styles.dot,
                  service.state === "loading"
                    ? styles.loading
                    : service.status?.reachable
                      ? styles.online
                      : styles.offline,
                ]
                  .filter(Boolean)
                  .join(" ")}
              />
              <strong>
                {service.state === "loading"
                  ? t("setup.checking")
                  : service.status?.status ?? t("common.offline")}
              </strong>
              <small>{service.status?.detail ?? t("setup.serviceHint")}</small>
            </div>
            <Button variant="icon" onClick={service.refresh} aria-label={t("setup.refresh")}>
              <RefreshCw size={18} />
            </Button>
          </>
        )}
      </section>
    </div>
  );
}
