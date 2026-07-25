import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

// The standard shadcn/ui helper: clsx for conditional classes, tailwind-merge to resolve
// conflicting utility classes (e.g. a caller's "p-4" overriding a default "p-2") rather
// than both landing in the className string and fighting on specificity.
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}
