import { type ChangeEvent, useRef, useState } from "react";
import { Mic, Square, Upload } from "lucide-react";
import { Button } from "../components/Button";
import { Panel } from "../components/Panel";
import { StatusPill } from "../components/StatusPill";
import { useI18n } from "../i18n";
import { useToast } from "../hooks/useToast";
import { useSettings } from "../hooks/useSettings";
import { streamTranscribe, transcribeFile, transcribeRemote } from "../lib/api";
import styles from "./CapturePage.module.css";

export function CapturePage() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const { settings } = useSettings();
  const isRemote = settings.asr_backend === "http";

  const [finalText, setFinalText] = useState("");
  const [interimText, setInterimText] = useState("");
  const [liveRunning, setLiveRunning] = useState(false);

  const [file, setFile] = useState<File | null>(null);
  const [fileResult, setFileResult] = useState("");
  const [fileBusy, setFileBusy] = useState(false);

  const abortRef = useRef<AbortController | null>(null);

  const startLive = () => {
    const controller = new AbortController();
    abortRef.current = controller;
    setLiveRunning(true);
    setFinalText("");
    setInterimText("");
    streamTranscribe(settings.asr_service_url, {
      signal: controller.signal,
      maxSeconds: 30,
      onPartial: (text) => setInterimText(text),
      onFinal: (text) => {
        setFinalText((prev) => (prev ? `${prev} ${text}` : text));
        setInterimText("");
      },
      onEnd: () => setLiveRunning(false),
    })
      .catch((err) => {
        console.error(err);
        showToast(t("toast.transcribeFailed"), "error");
      })
      .finally(() => setLiveRunning(false));
  };

  const stopLive = () => {
    abortRef.current?.abort();
    setLiveRunning(false);
  };

  const onFilePicked = (event: ChangeEvent<HTMLInputElement>) => {
    const picked = event.target.files?.[0] ?? null;
    setFile(picked);
    setFileResult("");
  };

  const runFile = async () => {
    if (!file) return;
    setFileBusy(true);
    setFileResult("");
    try {
      const result = isRemote
        ? await transcribeRemote(file, {
            url: settings.asr_service_url,
            apiKey: settings.asr_api_key,
            model: settings.asr_remote_model,
          })
        : await transcribeFile(file, settings.asr_service_url);
      setFileResult(result.text);
      showToast(t("toast.transcribeDone"), "success");
    } catch (err) {
      console.error(err);
      showToast(t("toast.transcribeFailed"), "error");
    } finally {
      setFileBusy(false);
    }
  };

  return (
    <div className={styles.page}>
      <div className={styles.grid}>
        <Panel
          title={t("capture.realtime")}
          aside={
            <StatusPill tone={isRemote ? "offline" : "online"}>
              {isRemote ? t("capture.backendRemote") : t("capture.backendLocal")}
            </StatusPill>
          }
        >
          <p className={styles.desc}>{t("capture.realtimeDesc")}</p>

          <div className={styles.transcript} aria-live="polite">
            {liveRunning || finalText || interimText ? (
              <p>
                {finalText}
                {interimText && <span className={styles.interim}>{interimText}</span>}
              </p>
            ) : (
              <p className={styles.placeholder}>{t("capture.realtimePlaceholder")}</p>
            )}
          </div>

          <div className={styles.actions}>
            {!isRemote &&
              (liveRunning ? (
                <Button variant="ghost" onClick={stopLive}>
                  <Square size={16} /> {t("capture.stop")}
                </Button>
              ) : (
                <Button onClick={startLive}>
                  <Mic size={16} /> {t("capture.start")}
                </Button>
              ))}
            {isRemote && <p className={styles.hint}>{t("capture.realtimeLocalOnly")}</p>}
          </div>
        </Panel>

        <Panel title={t("capture.file")}>
          <p className={styles.desc}>{t("capture.fileDesc")}</p>

          <label className={styles.fileButton}>
            <input type="file" accept="audio/*" className={styles.hiddenFile} onChange={onFilePicked} />
            <Upload size={16} /> {t("capture.choose")}
          </label>
          <p className={styles.fileName}>{file ? file.name : t("capture.filePlaceholder")}</p>
          {isRemote && <p className={styles.hint}>{t("capture.fileRemoteHint")}</p>}

          <Button fullWidth disabled={!file || fileBusy} onClick={runFile}>
            {fileBusy ? t("capture.transcribing") : t("capture.transcribe")}
          </Button>

          <div className={styles.transcript}>
            {fileResult ? (
              <>
                <span className={styles.resultLabel}>{t("capture.result")}</span>
                <p>{fileResult}</p>
              </>
            ) : (
              <p className={styles.placeholder}>{t("capture.realtimePlaceholder")}</p>
            )}
          </div>
        </Panel>
      </div>
    </div>
  );
}
