import { useCallback, useEffect, useState } from "react";
import { getAsrServiceStatus, type AsrServiceStatus } from "../lib/tauri";

type StatusState = "loading" | "idle" | "error";

export function useServiceStatus(enabled = true) {
  const [status, setStatus] = useState<AsrServiceStatus | null>(null);
  const [state, setState] = useState<StatusState>("loading");

  const refresh = useCallback(async () => {
    setState("loading");
    try {
      const result = await getAsrServiceStatus();
      setStatus(result);
      setState(result.reachable ? "idle" : "error");
    } catch (err) {
      setStatus({ reachable: false, url: "", status: "error", detail: String(err) });
      setState("error");
    }
  }, []);

  useEffect(() => {
    if (enabled) refresh();
  }, [enabled, refresh]);

  return { status, state, refresh };
}
