import type { ReactNode } from "react";

type Props = {
  /** sits in the top-left cutout, outside the card outline */
  title: ReactNode;
  /** right-aligned controls in the header row (eg a toggle) */
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
};

/**
 * card with the top-left corner cut out to hold the title. outline is built from non-overlapping pieces
 * (tab / strip / body) so nothing needs masking, holds up at any zoom / dpr. cutout itself has no border.
 * fixed sizes: header 56px (h-14), fillet 16px (w-4 / rounded-*-2xl), body offset 15px so its left edge starts where the fillet arc ends
 */
export default function NotchedPanel({
  title,
  actions,
  children,
  className = "",
}: Props) {
  return (
    <section className={`relative rounded-[22px] bg-white ${className}`}>

      <div className="flex h-14 items-stretch">

        <div className="relative flex items-center pl-4 pr-6">
          <h2 className="text-2xl font-semibold tracking-tight">{title}</h2>


          <span
            aria-hidden
            className="pointer-events-none absolute inset-0 left-4 top-4 rounded-br-[22px] border-b border-r border-neutral-200"
          />

          <span
            aria-hidden
            className="pointer-events-none absolute -right-[15px] top-0 h-4 w-4 rounded-tl-2xl border-l border-t border-neutral-200"
          />

          <span
            aria-hidden
            className="pointer-events-none absolute -bottom-[15px] left-0 h-4 w-4 rounded-tl-2xl border-l border-t border-neutral-200"
          />
        </div>


        <div className="relative ml-[15px] flex flex-1 items-center justify-end rounded-tr-[22px] border-r border-t border-neutral-200 pr-2.5">
          {actions}

          <span
            aria-hidden
            className="pointer-events-none absolute -right-px top-full h-[15px] border-r border-neutral-200"
          />
        </div>
      </div>


      <div className="mt-[15px] rounded-b-[22px] border-x border-b border-neutral-200 px-4 pb-4 pt-2">
        {children}
      </div>
    </section>
  );
}
