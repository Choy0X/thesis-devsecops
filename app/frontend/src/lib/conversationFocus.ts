import { useLocation } from "react-router-dom";
import { resolve } from "../routes";

// Whether the address names a project's conversation with the whole window (?focus=chat|agent&project=<id> on the Projects page), and which tab that is -- the one place this is
// decided, read by both the app shell (which chrome to take away) and the Projects page (what to render), so they cannot drift apart. False whenever the project id is
// missing/empty or the current route is not the Projects page, even if the address still says focus=chat|agent (a stale or hand-edited address, or a leftover param after a
// route change): the page then falls back to its normal content, inside its normal chrome. An address that names a session but no focus (an old agent-session deep link) still
// resolves its tab to "agent", so those links keep working; everything else defaults to "chat".

export function useConversationFocus(): { focused: boolean; projectId: string | null; tab: "chat" | "agent" } {
  const location = useLocation();
  const onProjects = resolve(location.pathname)?.sectionId === "projects";
  const params = new URLSearchParams(location.search);
  const projectId = params.get("project");
  const focusParam = params.get("focus");
  const tab: "chat" | "agent" = focusParam === "agent" ? "agent" : focusParam === "chat" ? "chat" : params.get("session") ? "agent" : "chat";
  const focused = onProjects && !!projectId && (focusParam === "chat" || focusParam === "agent");
  return { focused, projectId: focused ? projectId : null, tab };
}
