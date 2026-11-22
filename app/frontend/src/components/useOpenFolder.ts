import { api, errorText } from "../lib/api";
import { useToast } from "./Toast";

/** "Open" for one of the app's own folders: the file manager shows it. The server accepts a name, never a path. */
export function useOpenFolder(which: "models" | "interim") {
  const toast = useToast();
  return async () => {
    try {
      const r = await api.openFolder(which);
      if (!r.ok) toast.error(r.detail || "The folder could not be opened.");
    } catch (e) { toast.error(errorText(e)); }
  };
}
