"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

/**
 * shadcn-style Textarea — same visual posture as Input, multi-line.
 * Used for the playground's system prompt + user message composer.
 *
 * Pass ``rows`` to set the initial height. Resize is enabled by
 * default so operators can grow the box without us managing height
 * imperatively.
 */
const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.TextareaHTMLAttributes<HTMLTextAreaElement>
>(({ className, ...props }, ref) => {
  return (
    <textarea
      ref={ref}
      className={cn(
        "flex min-h-[80px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-sm shadow-sm",
        "placeholder:text-muted-foreground",
        "focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring",
        "disabled:cursor-not-allowed disabled:opacity-50",
        "font-mono",
        className,
      )}
      {...props}
    />
  );
});
Textarea.displayName = "Textarea";

export { Textarea };
