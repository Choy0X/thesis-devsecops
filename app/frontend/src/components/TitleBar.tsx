import { useEffect, useState } from "react";
import { Cpu, Copy, Minus, Square, X } from "lucide-react";
import { shell, whenReady } from "../lib/shellBridge";
import { IconButton } from "./kit/IconButton";
import { MODE_WORDS, modeTitle, useGpuMode } from "../lib/gpuMode";

// The window's own title bar, drawn by the page when the desktop shell opens a frameless window. It shows the product and the section, the GPU mode,
// and minimise, maximise or restore, and close, as real buttons with names. Double-clicking the bar toggles maximise. In a browser, in Chromium's
// app mode and when the window kept the operating system's frame there is no bridge to a frameless window, and nothing is drawn.

export const TITLEBAR_HEIGHT = "2.25rem";

export function TitleBar({ section }: { section: string }) {
  const [frameless, setFrameless] = useState(false);
  const [maximised, setMaximised] = useState(false);
  const gpu = useGpuMode(frameless);                                  // read only while the bar is drawn

  useEffect(() => {
    let alive = true;
    const stop = whenReady(async () => {
      const framed = await shell.hasFrame();
      if (alive && framed === false) setFrameless(true);              // undefined (no answer) means: draw nothing
    });
    return () => { alive = false; stop(); };
  }, []);

  useEffect(() => {
    if (!frameless) return;
    document.documentElement.style.setProperty("--titlebar-h", TITLEBAR_HEIGHT);
    let alive = true;
    const refresh = async () => { const m = await shell.isMaximized(); if (alive && typeof m === "boolean") setMaximised(m); };
    refresh();
    window.addEventListener("resize", refresh);
    return () => { alive = false; window.removeEventListener("resize", refresh); document.documentElement.style.setProperty("--titlebar-h", "0px"); };
  }, [frameless]);

  if (!frameless) return null;
  const toggle = async () => { await shell.toggleMaximize(); const m = await shell.isMaximized(); if (typeof m === "boolean") setMaximised(m); };
  // The three window buttons are full-height rectangles in the Windows manner, square and without a tooltip (their names are read out). Close is the danger tone: solid critical red
  // under the pointer, on keyboard focus and while pressed. The bar is a plain row at the top of the window's column, outside the scrolling page area.
  const button = "chamfer-none h-full w-[46px] border-0";
  return (
    <div role="banner" aria-label="Window" className="flex shrink-0 select-none items-stretch border-b border-line bg-surface" style={{ height: TITLEBAR_HEIGHT }}>
      <div data-pyloid-drag-region onDoubleClick={toggle} className="flex min-w-0 flex-1 items-center gap-3 px-3.5">
        <span data-pyloid-drag-region className="font-mono text-[0.68rem] font-semibold uppercase tracking-[0.18em] text-fg">Vuln Framework</span>
        <span data-pyloid-drag-region className="truncate text-[0.74rem] text-faint">{section}</span>
        <span data-pyloid-drag-region className="ml-auto inline-flex items-center gap-1.5 whitespace-nowrap text-[0.7rem] text-faint" title={modeTitle(gpu)}>
          <Cpu size={12} aria-hidden />{gpu ? MODE_WORDS[gpu.mode] : "GPU"}
        </span>
      </div>
      <IconButton label="Minimise" tip={false} className={button} onClick={() => shell.minimize()}><Minus size={15} aria-hidden /></IconButton>
      <IconButton label={maximised ? "Restore" : "Maximise"} tip={false} className={button} onClick={toggle}>
        {maximised ? <Copy size={13} aria-hidden /> : <Square size={12} aria-hidden />}
      </IconButton>
      <IconButton label="Close" tone="danger" tip={false} className={button} onClick={() => shell.close()}><X size={15} aria-hidden /></IconButton>
    </div>
  );
}
