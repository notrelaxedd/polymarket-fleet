import * as React from "react";
import { cn } from "@/lib/utils";

export function StatBlock({
  label,
  children,
  className,
}: {
  label: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex min-w-0 flex-col gap-0.5", className)}>
      <span className="label whitespace-nowrap">{label}</span>
      <span className="num flex items-baseline gap-1.5 whitespace-nowrap text-[14px] font-semibold leading-[18px]">{children}</span>
    </div>
  );
}
