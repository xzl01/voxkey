import styles from "./Skeleton.module.css";

interface SkeletonProps {
  width?: string;
  height?: string;
  radius?: string;
  circle?: boolean;
  className?: string;
}

export function Skeleton({ width, height, radius, circle, className }: SkeletonProps) {
  return (
    <span
      className={[styles.skeleton, circle ? styles.circle : "", className ?? ""].filter(Boolean).join(" ")}
      style={{ width, height, borderRadius: radius }}
      aria-hidden
    />
  );
}
