import { expect, test, type Page } from "@playwright/test";
import { fixtureFor } from "./fixtures";

// The Campaigns page in the built app, with the API answered by fixtures: the list, the start dialog in its three steps (what was judged is what starts), a refusal
// that lists its reasons, and the banner for an interrupted campaign.

const ID = "cmp-20261010-aaaa";
const row = (over: Record<string, unknown> = {}) => ({
  campaign_id: ID, name: "npm first", state: "running", created_at: "2026-10-10T10:00:00Z", started_at: "2026-10-10T10:00:00Z", finished_at: null, heartbeat_ts: 1, control: "run", job_id: "j", pid: 1,
  resumes: 0, error: null, ecosystems: ["npm"], progress: { target: "npm", stage: "mine", step: 3, steps: 4, percent: 62.5 }, summary: {}, declared: {}, legacy: false,
  plan: { name: "npm first", ecosystems: ["npm"], preset: "collect", stages: { ingest: true, queue: true, recover: true, mine: true }, knobs: {}, build_profile: null, context: { profile: "weak", workers: 2, cap: null } }, ...over,
});
const STAGES = ["ingest", "queue", "recover", "mine", "verify", "resplit", "dep_versions", "enrich", "build"];
const options = {
  targets: [{ name: "npm", downloaded: true, ingested: true, dump_bytes: 400_000_000, snapshot: null, mineable: true, reason: null, owner: null, stopped: null },
            { name: "Ubuntu", downloaded: true, ingested: true, dump_bytes: 770_000_000, snapshot: null, mineable: false, reason: "No mining adapter.", owner: null, stopped: null }],
  presets: [{ id: "collect", name: "Collect", stages: ["ingest", "queue", "recover", "mine"] }, { id: "full", name: "Full", stages: STAGES.slice(0, 7) }],
  stages: STAGES.map((id) => ({ id, label: id, scope: id === "build" ? "campaign" : "ecosystem" })), knobs: { mine_workers: 4, pause_seconds: 10 }, knob_limits: {},
  build_profiles: [{ id: "training", name: "Training", description: "d" }], context_profiles: [{ id: "weak", name: "Weak", description: "d" }], context_defaults: { profile: "weak", workers: 2, cap: null },
  context_workers: [1, 4], default_preset: "collect",
};
const verdict = (ok = true) => ({ ok, plan: {}, errors: ok ? [] : [{ code: "ram_free", text: "Not enough free memory.", source: "machine" }], warnings: [],
  per_ecosystem: { npm: { stages: ["ingest", "queue", "recover", "mine"], notes: [] } }, needs: { cpu: 2, ram_gb: 1.4, github_per_hour: 2800, disk_gb: 15, context: false, build: false },
  free: { cpu: 5, ram_gb: 9, github_per_hour: 4500, disk_gb: 180, jobs: 4 } });

const nums = (a: number, b: number, c: number, d: number, e: number) => ({ advisories: a, with_fix_pointer: b, mined: c, pairs: d, usable_pairs: e });
const analytics = {
  campaign_id: ID, name: "npm first", state: "partial", legacy: false, ecosystems: ["npm"], created_at: "2026-10-10T10:00:00Z", started_at: "2026-10-10T10:00:00Z", finished_at: "2026-10-10T10:30:00Z", generated_at: "2026-10-10T10:30:00Z", note: null,
  timeline: { steps: [{ target: "npm", stage: "ingest", label: "Read", status: "done", attempts: 1, started_at: "2026-10-10T10:00:00Z", finished_at: "2026-10-10T10:02:05Z", seconds: 125, error: null, eta_seconds: null, fraction: null },
                      { target: "npm", stage: "mine", label: "Mine", status: "error", attempts: 2, started_at: "2026-10-10T10:03:00Z", finished_at: "2026-10-10T10:20:00Z", seconds: 1020, error: "x", eta_seconds: null, fraction: null }],
    total: 2, done: 1, errors: 1, percent: 50, elapsed_seconds: 1800, active_seconds: 1145 },
  errors: { by_cause: [{ cause: "network", n: 2 }], latest: [], commits: [{ status: "no_code", reason: "no_source_files", n: 4 }], forced_kills: 0, interruptions: 0 },
  funnel: { ecosystems: { npm: { before: nums(0, 0, 0, 0, 0), now: nums(100, 50, 20, 50, 40), gained: nums(100, 50, 20, 50, 40) } }, total: nums(100, 50, 20, 50, 40), has_baseline: true,
            flow: { stages: [{ id: "advisories", label: "Advisories read", unit: "advisories", count: 100, lost: [] }, { id: "usable_pairs", label: "Usable code pairs", unit: "pairs", count: 40, lost: [] }] } },
  throughput: { bucket_seconds: 600, per_hour: { commits: 120, pairs: 80 }, buckets: [{ at: "2026-10-10T10:10:00Z", commits: 5, pairs: 3 }, { at: "2026-10-10T10:20:00Z", commits: 9, pairs: 6 }],
                samples: [{ at: "2026-10-10T10:10:00Z", rss_mb: 300, pending: 50, remaining: 4000, waiting: false }, { at: "2026-10-10T10:20:00Z", rss_mb: 340, pending: 36, remaining: 3800, waiting: false }], waits: [] },
  mix: { pairs: 40, commits: 20, language: { python: 25, javascript: 15 }, granularity: { file: 20, function: 20 }, ecosystem: { npm: 40 }, cwe: { "CWE-79": 12 }, severity: { high: 30, critical: 10 } }, context: null,
};

async function mock(page: Page, state: { campaigns: unknown[]; created?: unknown; refuse?: boolean }) {
  await page.route("**/api/**", async (route) => {
    const url = new URL(route.request().url()), path = url.pathname, method = route.request().method();
    const json = (body: unknown, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (path === "/api/campaigns" && method === "GET") return json({ campaigns: state.campaigns, interrupted: [], owners: {} });
    if (path === "/api/campaigns/options") return json(options);
    if (path === "/api/campaigns/validate") return json(verdict());
    if (path === "/api/campaigns/resources") return json({ campaigns: [] });
    if (path === "/api/campaigns" && method === "POST") {
      state.created = route.request().postDataJSON();
      if (state.refuse) return json({ ok: false, error: "Not enough room to start this campaign", detail: "x", hint: null, code: "no_room", reasons: [{ code: "cpu", text: "No processor core is free." }] }, 409);
      state.campaigns = [row()];
      return json({ ok: true, campaign: row(), job: {}, warnings: [] }, 201);
    }
    if (path === `/api/campaigns/${ID}`) return json({ campaign: row({ state: "partial", finished_at: "2026-10-10T10:30:00Z" }), job: null, steps: [
      { target: "npm", stage: "ingest", status: "done", started_at: "2026-10-10T10:00:00Z", finished_at: "2026-10-10T10:02:05Z", detail: { rows: 12345 }, error: null, attempts: 1, progress: {} },
      { target: "npm", stage: "mine", status: "error", started_at: "2026-10-10T10:03:00Z", finished_at: "2026-10-10T10:20:00Z", detail: {}, error: "ConnectionError: the network is down", attempts: 2, progress: {} }] });
    if (path === `/api/campaigns/${ID}/analytics`) return json(analytics);
    if (path === `/api/campaigns/${ID}/events`) return json({ last: 3, events: [{ id: 1, at: "2026-10-10T10:00:00Z", kind: "created", data: { ecosystems: ["npm"] } },
      { id: 3, at: "2026-10-10T10:20:00Z", kind: "stage_error", data: { target: "npm", stage: "mine", cause: "network", message: "down" } }] });
    return json(fixtureFor(method, path) ?? {});
  });
}

test("a campaign is started from the dialog with the plan that was checked", async ({ page }) => {
  const state: { campaigns: unknown[]; created?: unknown } = { campaigns: [] };
  await mock(page, state);
  await page.goto("/campaigns");
  await expect(page.getByText("No campaigns yet")).toBeVisible();
  await page.getByRole("button", { name: "Start a campaign" }).click();
  const dialog = page.getByRole("dialog", { name: "Start a campaign" });
  await expect(dialog.getByRole("button", { name: "Next" })).toBeDisabled();
  await dialog.getByLabel(/^npm/).check();
  await dialog.getByRole("button", { name: "Next" }).click();
  await expect(dialog.getByLabel("Collect")).toBeChecked();
  await dialog.getByRole("button", { name: "Next" }).click();
  await expect(dialog.getByText("This plan can start now.")).toBeVisible();
  await expect(dialog.getByRole("table", { name: "What the campaign needs and what is free" })).toBeVisible();
  await dialog.getByRole("button", { name: "Start the campaign" }).click();
  await expect(page.getByRole("heading", { name: /npm first/ })).toBeVisible();
  await expect(page).toHaveURL(/\/campaigns\?c=cmp-20261010-aaaa/);
  expect(state.created).toMatchObject({ ecosystems: ["npm"], preset: "collect", stages: { ingest: true, mine: true, verify: false } });
});

test("a start the computer cannot take lists its reasons and leaves the dialog open", async ({ page }) => {
  await mock(page, { campaigns: [], refuse: true });
  await page.goto("/campaigns?start=npm");
  const dialog = page.getByRole("dialog", { name: "Start a campaign" });
  await expect(dialog.getByLabel(/^npm/)).toBeChecked();
  await dialog.getByRole("button", { name: "Next" }).click();
  await dialog.getByRole("button", { name: "Next" }).click();
  await dialog.getByRole("button", { name: "Start the campaign" }).click();
  await expect(dialog.getByText("No processor core is free.")).toBeVisible();
  await expect(dialog).toBeVisible();
});

test("the list shows a running campaign and the buttons that fit it", async ({ page }) => {
  await mock(page, { campaigns: [row(), row({ campaign_id: "cmp-20261010-bbbb", name: "PyPI", state: "paused" })] });
  await page.goto("/campaigns");
  const table = page.getByRole("table", { name: "Campaigns" });
  await expect(table.getByText("cmp-20261010-aaaa")).toBeVisible();
  await expect(table.getByRole("button", { name: "Pause cmp-20261010-aaaa" })).toBeVisible();
  await expect(table.getByRole("button", { name: "Resume cmp-20261010-bbbb" })).toBeVisible();
  await expect(table.getByRole("progressbar").first()).toHaveAttribute("aria-valuetext", "63 percent");
});

test("a campaign that was interrupted has a banner with one button", async ({ page }) => {
  await mock(page, { campaigns: [row({ state: "interrupted" })] });
  await page.goto("/campaigns");
  const banner = page.getByRole("alert", { name: "Interrupted campaigns" });
  await expect(banner).toContainText("nothing restarted by itself");
  await expect(banner.getByRole("button", { name: "Resume cmp-20261010-aaaa" })).toBeVisible();
});

test("a campaign opens with its steps, its story and its plan", async ({ page }) => {
  await mock(page, { campaigns: [row({ state: "partial" })] });
  await page.goto("/campaigns");
  await page.getByText("npm first").click();
  await expect(page).toHaveURL(/c=cmp-20261010-aaaa/);
  const steps = page.getByRole("table", { name: "The steps of the campaign" });
  await expect(steps.getByText("ConnectionError: the network is down")).toBeVisible();
  await expect(steps.getByText("rows 12,345")).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry the failed steps" })).toBeVisible();
  await page.getByRole("tab", { name: "Log" }).click();
  await expect(page.getByRole("list", { name: "Events" }).getByText("Campaign created for npm.")).toBeVisible();
  await expect(page.getByRole("list", { name: "Events" }).getByText(/Failed: Mine the code pairs \(npm\), network: down/)).toBeVisible();
  await page.getByRole("tab", { name: "Config" }).click();
  await expect(page.getByText("As it was chosen when the campaign started.")).toBeVisible();
  await expect(page).toHaveURL(/tab=config/);
});

test("the analytics of a campaign show its charts, each with its table", async ({ page }) => {
  await mock(page, { campaigns: [row({ state: "partial" })] });
  await page.goto("/campaigns?c=cmp-20261010-aaaa&tab=analytics");
  await expect(page.getByText("Progress and stage timeline")).toBeVisible();
  await expect(page.getByRole("list", { name: "Steps on a time line" }).getByRole("listitem")).toHaveCount(2);
  await expect(page.getByText("What the campaign added", { exact: true })).toBeVisible();
  await expect(page.getByText("Commits and pairs over time")).toBeVisible();
  await expect(page.getByLabel("python: 25 pairs, 63 percent")).toBeVisible();
  await expect(page.getByLabel("network: 2 steps, 100 percent")).toBeVisible();
  const timeline = page.locator("figure", { hasText: "Progress and stage timeline" });
  await timeline.getByRole("button", { name: "View as table" }).click();
  await expect(timeline.getByRole("table", { name: "The steps of the campaign" })).toBeVisible();
});
