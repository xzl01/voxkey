import { lazy, Suspense, useState } from "react";
import { Sidebar, type PageId } from "./components/Sidebar";
import { TopBar } from "./components/TopBar";
import { Skeleton } from "./components/Skeleton";
import { useI18n } from "./i18n";
import styles from "./App.module.css";

const SetupPage = lazy(() => import("./pages/SetupPage").then((m) => ({ default: m.SetupPage })));
const ModelsPage = lazy(() => import("./pages/ModelsPage").then((m) => ({ default: m.ModelsPage })));
const CapturePage = lazy(() => import("./pages/CapturePage").then((m) => ({ default: m.CapturePage })));
const ServicePage = lazy(() => import("./pages/ServicePage").then((m) => ({ default: m.ServicePage })));

const PAGE_META: Record<
  PageId,
  { title: "setup.title" | "models.title" | "capture.title" | "service.title"; subtitle: "setup.subtitle" | "models.subtitle" | "capture.subtitle" | "service.subtitle" }
> = {
  setup: { title: "setup.title", subtitle: "setup.subtitle" },
  models: { title: "models.title", subtitle: "models.subtitle" },
  capture: { title: "capture.title", subtitle: "capture.subtitle" },
  service: { title: "service.title", subtitle: "service.subtitle" },
};

function PageSkeleton() {
  return (
    <div className={styles.skeletonWrap}>
      <Skeleton height="140px" radius="var(--radius-md)" />
      <Skeleton height="180px" radius="var(--radius-md)" />
      <Skeleton height="120px" radius="var(--radius-md)" />
    </div>
  );
}

export function App() {
  const { t } = useI18n();
  const [page, setPage] = useState<PageId>("setup");
  const meta = PAGE_META[page];

  return (
    <div className={styles.shell}>
      <Sidebar active={page} onNavigate={setPage} />
      <main className={styles.workspace}>
        <TopBar title={t(meta.title)} subtitle={t(meta.subtitle)} />
        <Suspense fallback={<PageSkeleton />}>
          {page === "setup" && <SetupPage onNavigate={setPage} />}
          {page === "models" && <ModelsPage />}
          {page === "capture" && <CapturePage />}
          {page === "service" && <ServicePage />}
        </Suspense>
      </main>
    </div>
  );
}
