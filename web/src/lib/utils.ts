import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/**
 * Tailwind className merge helper. Combines clsx (conditional classes)
 * with tailwind-merge (resolves conflicting Tailwind utilities, e.g.
 * `px-2 px-4` becomes `px-4`).
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
