import { expect, test } from "@playwright/test";
import { fixtureFor } from "./fixtures";

test("Ollama Model Hub shows the installed inventory and keeps the import form usable at compact width", async ({ page }, info) => {
  await page.setViewportSize({ width: 390, height: 840 });
  await page.route("**/api/**", async (route) => {
    const method = route.request().method();
    const { pathname } = new URL(route.request().url());
    const body = pathname === "/api/serving/ollama/models" ? {
      status: "ready",
      models: [
        { tag: "aivf/reviewer-v40:latest", name: "AIVF Reviewer v40", digest: "a".repeat(64), capabilities: ["completion"], location: "local", available: true, size: 3 * 1024 ** 3 },
        { tag: "gpt-oss:120b-cloud", name: "GPT OSS", digest: "b".repeat(64), capabilities: ["completion"], location: "cloud", available: true, size: 0 },
      ],
    } : fixtureFor(method, pathname) ?? {};
    await route.fulfill({ json: body });
  });

  await page.goto("/models");
  const local = page.getByRole("table", { name: "Ollama local models" });
  await expect(local.getByText("AIVF Reviewer v40")).toBeVisible();
  await expect(local.getByText("aivf/reviewer-v40:latest")).toBeVisible();
  await expect(local.getByText("gpt-oss:120b-cloud")).toHaveCount(0);
  await expect(page.getByRole("table", { name: "Ollama Cloud models" })).toContainText("gpt-oss:120b-cloud");

  const add = page.getByRole("button", { name: "Add model to Ollama" });
  await add.click();
  const dialog = page.getByRole("dialog", { name: "Add model to Ollama" });
  await dialog.getByRole("radio", { name: "Hugging Face GGUF" }).check();
  await dialog.getByLabel("Repository", { exact: true }).fill("owner/model-gguf");
  await expect(dialog.getByRole("button", { name: "Install model" })).toBeDisabled();
  await dialog.getByLabel("GGUF filename").fill("model-Q4_K_M.gguf");
  await expect(dialog.getByRole("button", { name: "Install model" })).toBeEnabled();
  expect(await dialog.evaluate((el) => el.getBoundingClientRect().right <= window.innerWidth)).toBe(true);
  await dialog.screenshot({ path: info.outputPath("ollama-install-compact.png") });
  await page.keyboard.press("Escape");
  await expect(add).toBeFocused();
});

test("the catalog keeps GGUF installation available when model dimensions are missing", async ({ page }, info) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const repo = "jinaai/jina-code-embeddings-0.5b-GGUF";
  const file = "jina-code-embeddings-0.5b-Q4_K_M.gguf";
  const revision = "a".repeat(40);
  await page.route("**/api/**", async (route) => {
    const method = route.request().method();
    const { pathname } = new URL(route.request().url());
    const responses: Record<string, unknown> = {
      "/api/models/search": { query: "jina", task: "embedding", models: [{
        id: repo, task: "feature-extraction", downloads: 5, likes: 0, gated: false, parameters: null,
      }] },
      "/api/models/download-plan": {
        repo_id: repo, revision, gated: false, quants: [], needs_quant: false, files: 1, bytes: 390_000_000, free_bytes: 100_000_000_000,
        enough: true, gguf_files: [{ name: file, bytes: 390_000_000 }],
      },
      "/api/models/fit": {
        mode: "inference", estimate_complete: false, parts: { weights: 390_000_000 }, total: 390_000_000,
        notes: ["This embedding model's memory use depends on how Ollama runs it."],
        check: { gpu: "unknown", ram: "unknown", gpu_free_bytes: null, ram_free_bytes: 8_000_000_000, disk: "unknown", runs_on: "nowhere" },
      },
    };
    await route.fulfill({ json: responses[pathname] ?? fixtureFor(method, pathname) ?? {} });
  });
  await page.goto("/models?tab=catalog");
  await expect(page).toHaveURL(/models\?tab=catalog/);
  await page.getByLabel("Search models").fill("jina");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await page.getByText(repo, { exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Does jina-code-embeddings-0.5b-GGUF fit?" });
  await expect(dialog.getByText("Runtime memory estimate unavailable")).toBeVisible();
  await expect(dialog.getByText("Model file size", { exact: true })).toBeVisible();
  await expect(dialog.getByText(/Fine-tune it|does not state: params/)).toHaveCount(0);
  expect(errors).toEqual([]);
  await dialog.screenshot({ path: info.outputPath("gguf-install-desktop.png") });

  await page.setViewportSize({ width: 390, height: 840 });
  const install = dialog.getByRole("button", { name: "Install in Ollama" });
  await expect(install).toBeEnabled();
  expect(await dialog.evaluate((el) => el.getBoundingClientRect().right <= window.innerWidth)).toBe(true);
  await dialog.screenshot({ path: info.outputPath("gguf-install-compact.png") });
  await install.click();
  const confirmation = page.getByRole("alertdialog");
  await expect(confirmation).toContainText(file);
  await expect(confirmation.getByRole("button", { name: "Install in Ollama" })).toBeEnabled();
});
