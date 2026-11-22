import { useEffect, useRef, useState, type ReactNode } from "react";

/** Draws its children, and so lets them load their data, only once they are about to scroll into view. A panel far down a long page
 *  that sends heavy requests on mount makes every visit pay for something the reader may never reach. A page that is hidden has no
 *  size, so nothing inside it counts as visible. Once shown, the children stay. Without IntersectionObserver they show at once. */
export function WhenVisible({ children, height = 120, margin = 300 }: { children: ReactNode; height?: number; margin?: number }) {
  const box = useRef<HTMLDivElement>(null);
  const [seen, setSeen] = useState(typeof IntersectionObserver === "undefined");
  useEffect(() => {
    const el = box.current;
    if (seen || !el) return;
    const watcher = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { setSeen(true); watcher.disconnect(); }
    }, { rootMargin: `${margin}px` });
    watcher.observe(el);
    return () => watcher.disconnect();
  }, [seen, margin]);
  if (seen) return <>{children}</>;
  return (
    <div ref={box} style={{ minHeight: height }}
      className="mt-4 grid place-items-center rounded-md border border-dashed border-line text-[0.76rem] text-faint">
      Loads when you scroll here.
    </div>
  );
}
