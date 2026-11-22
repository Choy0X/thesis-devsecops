import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { reconnectingSocket, stateWords, type SocketState } from "./reconnectingSocket";

class Fake {
  static all: Fake[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;
  constructor(public url: string) { Fake.all.push(this); }
  close() { this.closed = true; }
}

beforeEach(() => { Fake.all = []; vi.useFakeTimers(); });
afterEach(() => vi.useRealTimers());

const start = (delays = [1000, 2000]) => {
  const states: [SocketState, number | undefined][] = [];
  const got: string[] = [];
  const stop = reconnectingSocket({ url: "ws://x", delays, make: (u) => new Fake(u) as unknown as WebSocket, onMessage: (d) => got.push(d), onState: (s, r) => states.push([s, r]) });
  return { stop, states, got };
};

describe("reconnectingSocket", () => {
  it("goes live when it opens and passes messages on", () => {
    const { states, got } = start();
    Fake.all[0].onopen!();
    Fake.all[0].onmessage!({ data: "hello" });
    expect(states.map((s) => s[0])).toEqual(["connecting", "live"]);
    expect(got).toEqual(["hello"]);
  });

  it("opens again after a drop, waiting longer each time, and starts over once it is live", () => {
    const { states } = start([1000, 2000]);
    Fake.all[0].onclose!();
    expect(states[states.length - 1]).toEqual(["reconnecting", 1000]);
    vi.advanceTimersByTime(999);
    expect(Fake.all).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(Fake.all).toHaveLength(2);
    Fake.all[1].onclose!();
    expect(states[states.length - 1]).toEqual(["reconnecting", 2000]);
    vi.advanceTimersByTime(2000);
    Fake.all[2].onopen!();
    Fake.all[2].onclose!();
    expect(states[states.length - 1]).toEqual(["reconnecting", 1000]);
  });

  it("does nothing more once it is closed on purpose", () => {
    const { stop, states } = start();
    Fake.all[0].onopen!();
    stop();
    expect(Fake.all[0].closed).toBe(true);
    vi.advanceTimersByTime(60_000);
    expect(Fake.all).toHaveLength(1);
    expect(states[states.length - 1][0]).toBe("closed");
  });

  it("cancels a pending retry when it is closed", () => {
    const { stop } = start();
    Fake.all[0].onclose!();
    stop();
    vi.advanceTimersByTime(60_000);
    expect(Fake.all).toHaveLength(1);
  });

  it("puts each state in plain words", () => {
    expect(stateWords("live")).toBe("Live");
    expect(stateWords("reconnecting", 2000)).toBe("Connection lost. Trying again in 2 s.");
    expect(stateWords("closed")).toBe("Closed");
  });
});
