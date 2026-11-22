// Links built from data that came from outside: advisory text, repository addresses read from git or from an advisory. A value such as
// "javascript:...//github.com" must never become the target of a link, so a link is made only for an http or https address, and a commit link only for a
// repository on GitHub or GitLab with a hexadecimal commit id.

/** The address if it is a plain http or https address, otherwise null. */
export function webUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const u = new URL(value.trim());
    return u.protocol === "https:" || u.protocol === "http:" ? u.toString().replace(/\/$/, "") : null;
  } catch {                                                       // a value that is not an address at all has no link
    return null;
  }
}

const HOSTS = new Set(["github.com", "www.github.com", "gitlab.com", "www.gitlab.com"]);

/** The page of a commit on GitHub or GitLab, or null when the repository is elsewhere, the address is not safe or the id is not a commit id. */
export function commitUrl(repoUrl: string | null | undefined, sha: string | null | undefined): string | null {
  const base = webUrl(repoUrl);
  if (!base || !sha || !/^[0-9a-f]{7,64}$/i.test(sha)) return null;
  const host = new URL(base).hostname.toLowerCase();
  if (!HOSTS.has(host)) return null;
  return `${base}${host.endsWith("gitlab.com") ? "/-" : ""}/commit/${sha}`;
}
