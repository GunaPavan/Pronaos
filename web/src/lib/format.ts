/**
 * Small formatting helpers shared across the dashboard.
 * Centralizing them here keeps cost / token / time conventions
 * identical everywhere they show up.
 */

/** Format a Pronaos hcents amount (hundredths of a cent) as USD. */
export function formatHcents(hcents: number): string {
  const dollars = hcents / 10_000;
  if (dollars >= 1000) {
    return `$${dollars.toFixed(0)}`;
  }
  if (dollars >= 1) {
    return `$${dollars.toFixed(2)}`;
  }
  if (dollars >= 0.01) {
    return `$${dollars.toFixed(3)}`;
  }
  return `$${dollars.toFixed(4)}`;
}

/** Compact token count: 1234 → "1.2k", 1_234_567 → "1.23M". */
export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** Unix-second bucket → "Jan 15" or "14:00" depending on bucket size. */
export function formatBucket(epochSeconds: number, bucketSizeSeconds: number): string {
  const d = new Date(epochSeconds * 1000);
  if (bucketSizeSeconds === 3600) {
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** Days remaining until an epoch-second moment, floored at 0. */
export function daysUntil(epochSeconds: number): number {
  const ms = epochSeconds * 1000 - Date.now();
  return Math.max(0, Math.ceil(ms / 86_400_000));
}

/** Percent of budget consumed, clamped 0..100. */
export function budgetPct(current: number, cap: number | null): number {
  if (cap == null || cap <= 0) return 0;
  return Math.min(100, Math.round((current / cap) * 100));
}
