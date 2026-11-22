import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { ToastProvider, useToast } from "../components/Toast";
import { Dev, DevModeProvider, useDevMode } from "./devMode";

beforeEach(() => window.localStorage.clear());

function Switch() {
  const [on, set] = useDevMode();
  return <button onClick={() => set(!on)}>{on ? "turn off" : "turn on"}</button>;
}

describe("Developer mode", () => {
  it("is off by default and shows <Dev> content only while on", () => {
    render(<DevModeProvider><Switch /><Dev><p>job id 4f2a9c</p></Dev></DevModeProvider>);
    expect(screen.queryByText("job id 4f2a9c")).toBeNull();
    fireEvent.click(screen.getByText("turn on"));
    expect(screen.getByText("job id 4f2a9c")).toBeInTheDocument();
  });

  it("is remembered on this computer", () => {
    const first = render(<DevModeProvider><Switch /></DevModeProvider>);
    fireEvent.click(screen.getByText("turn on"));
    first.unmount();
    render(<DevModeProvider><Switch /></DevModeProvider>);
    expect(screen.getByText("turn off")).toBeInTheDocument();
  });

  it("starts off when the storage cannot be read", () => {
    const spy = Object.getOwnPropertyDescriptor(window, "localStorage")!;
    Object.defineProperty(window, "localStorage", { get: () => { throw new Error("blocked"); }, configurable: true });
    try {
      render(<DevModeProvider><Switch /></DevModeProvider>);
      expect(screen.getByText("turn on")).toBeInTheDocument();
      fireEvent.click(screen.getByText("turn on"));                          // and a blocked write does not break the switch
      expect(screen.getByText("turn off")).toBeInTheDocument();
    } finally {
      Object.defineProperty(window, "localStorage", spy);
    }
  });
});

function Failing() {
  const toast = useToast();
  return <button onClick={() => toast.error("The records could not be saved", "409 conflict {\"ok\":false}")}>fail</button>;
}

describe("error toasts", () => {
  it("show the plain message to everyone and the technical details only in Developer mode", () => {
    const { unmount } = render(<DevModeProvider initial={false}><ToastProvider><Failing /></ToastProvider></DevModeProvider>);
    fireEvent.click(screen.getByText("fail"));
    expect(screen.getByText(/The records could not be saved/)).toBeInTheDocument();
    expect(screen.queryByText("Technical details")).toBeNull();
    expect(screen.queryByText(/409 conflict/)).toBeNull();
    unmount();
    render(<DevModeProvider initial><ToastProvider><Failing /></ToastProvider></DevModeProvider>);
    act(() => { fireEvent.click(screen.getByText("fail")); });
    expect(screen.getByText("Technical details")).toBeInTheDocument();
    expect(screen.getByText(/409 conflict/)).toBeInTheDocument();
  });
});
