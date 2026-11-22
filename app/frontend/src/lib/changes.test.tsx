import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./request", async () => {
  const actual = await vi.importActual<typeof import("./request")>("./request");
  return { ...actual, clearRequestCache: vi.fn(), invalidateRequestResource: vi.fn() };
});
import { clearRequestCache, invalidateRequestResource } from "./request";
import { notifyChanged, resetChanges, subscribeChanged, topicsOfFinished, useOnChanged } from "./changes";
import type { Job } from "./api";

const job = (id: string, stage: string, status = "done"): Job => ({ id, stage, status, params: {}, started: "", finished: "", error: "", log_count: 0 });

beforeEach(() => { resetChanges(); vi.mocked(clearRequestCache).mockClear(); vi.mocked(invalidateRequestResource).mockClear(); });

describe("which jobs change what the app shows", () => {
  it("says nothing the first time it sees the list: jobs that were already over changed nothing now", () => {
    const first = topicsOfFinished([job("a", "hf_download"), job("b", "install_llama_cpp")], null);
    expect(first.topics).toEqual([]);
    expect(topicsOfFinished([job("a", "hf_download"), job("b", "install_llama_cpp")], first.seen).topics).toEqual([]);
  });

  it("refreshes models for downloads, conversions and imports", () => {
    const seen = topicsOfFinished([], null).seen;
    for (const [stage, topic] of [["hf_download", "models"], ["install_model", "models"], ["convert_gguf", "models"], ["convert_adapter", "models"], ["import_finetune_outputs", "models"]]) {
      expect(topicsOfFinished([job(`j-${stage}`, stage)], seen).topics, stage).toEqual([topic]);
    }
  });

  it("tells once for a job, also when it started and ended between two looks, and not at all while it runs", () => {
    const first = topicsOfFinished([], null);
    const running = topicsOfFinished([job("x", "hf_download", "running")], first.seen);
    expect(running.topics).toEqual([]);
    const done = topicsOfFinished([job("x", "hf_download", "done")], running.seen);
    expect(done.topics).toEqual(["models"]);
    expect(topicsOfFinished([job("x", "hf_download", "done")], done.seen).topics).toEqual([]);
    const short = topicsOfFinished([job("y", "hf_download", "done")], done.seen);              // never seen running: it was over before the next look
    expect(short.topics).toEqual(["models"]);
  });

  it("counts a download that failed or was stopped too, since part of it is on the disk, and ignores other stages", () => {
    const seen = topicsOfFinished([], null).seen;
    expect(topicsOfFinished([job("e", "hf_download", "error"), job("c", "hf_download", "cancelled")], seen).topics).toEqual(["models"]);
    expect(topicsOfFinished([job("o", "ingest"), job("p", "mine_code")], seen).topics).toEqual([]);
  });

  it("lists each topic once however many jobs ended", () => {
    const seen = topicsOfFinished([], null).seen;
    expect(topicsOfFinished([job("1", "hf_download"), job("2", "convert_gguf"), job("3", "install_llama_cpp")], seen).topics.sort()).toEqual(["models"]);
  });
});

describe("telling the page", () => {
  it("forgets what the page kept, then calls the ones that listen to that topic and no others", () => {
    const models = vi.fn(), program = vi.fn();
    subscribeChanged(["models"], models);
    subscribeChanged(["program"], program);
    notifyChanged("models");
    expect(invalidateRequestResource).toHaveBeenCalledWith('/api/models');
    expect(clearRequestCache).not.toHaveBeenCalled();
    expect(models).toHaveBeenCalledTimes(1);
    expect(program).not.toHaveBeenCalled();
    notifyChanged("models", "program");
    expect(clearRequestCache).toHaveBeenCalledTimes(1);
    expect(models).toHaveBeenCalledTimes(2);
    expect(program).toHaveBeenCalledTimes(1);
  });

  it("stops calling after unsubscribing", () => {
    const fn = vi.fn();
    const off = subscribeChanged(["models"], fn);
    off();
    notifyChanged("models");
    expect(fn).not.toHaveBeenCalled();
  });

  it("is a hook that calls the latest function while mounted and not after", () => {
    const first = vi.fn(), second = vi.fn();
    const { rerender, unmount } = renderHook(({ fn }) => useOnChanged(["models", "program"], fn), { initialProps: { fn: first } });
    rerender({ fn: second });
    notifyChanged("program");
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
    unmount();
    notifyChanged("models");
    expect(second).toHaveBeenCalledTimes(1);
  });

  it("one subscriber hears one notification once even when it listens to both topics and both changed", () => {
    const fn = vi.fn();
    subscribeChanged(["models", "program"], fn);
    notifyChanged("models", "program");
    expect(fn).toHaveBeenCalledTimes(1);
  });
});
