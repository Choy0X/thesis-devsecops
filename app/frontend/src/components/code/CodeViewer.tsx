import { lazy, Suspense, type ReactNode } from "react";
import type { CodeViewerProps } from "./CodeViewerImpl";

// A file's content drawn by CodeMirror 6, read-only, with the line numbers the file really has, loaded when it is first needed. The editor's code is a separate chunk (the dynamic import
// below): a page that never shows a file never loads it, and while it loads, and wherever it cannot run, ``fallback`` (the same text as plain text) is shown. It cannot run where the DOM cannot
// measure text: the tests' jsdom has no ``Range.getClientRects``, so there the fallback is what is drawn, and a test reads the text it always read.

const Viewer = lazy(() => import("./CodeViewerImpl"));

export function canMountEditor(): boolean {
  return typeof document !== "undefined" && typeof Range !== "undefined" && typeof Range.prototype.getClientRects === "function";
}

export function CodeViewer({ fallback, ...props }: CodeViewerProps & { fallback: ReactNode }) {
  if (!canMountEditor()) return <>{fallback}</>;
  return <Suspense fallback={fallback}><Viewer {...props} /></Suspense>;
}
