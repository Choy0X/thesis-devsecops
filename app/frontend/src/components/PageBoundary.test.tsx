import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DevModeProvider } from "../lib/devMode";
import { PageBoundary } from "./PageBoundary";

let explode = true;
function Bomb() {
  if (explode) throw new Error("the answer had no list");
  return <p>the page is fine</p>;
}

beforeEach(() => { explode = true; vi.spyOn(console, "error").mockImplementation(() => undefined); });
afterEach(() => vi.restoreAllMocks());

const show = (dev = false) => render(<DevModeProvider initial={dev}><PageBoundary name="Records"><Bomb /></PageBoundary></DevModeProvider>);

describe("PageBoundary", () => {
  it("says the page hit an error and that the rest of the app works, without the technical text", () => {
    show();
    expect(screen.getByRole("alert")).toHaveTextContent("This page hit an error and could not be drawn.");
    expect(screen.getByRole("alert")).toHaveTextContent("The rest of the app still works.");
    expect(screen.queryByText(/the answer had no list/)).toBeNull();
  });

  it("shows the technical text in Developer mode", () => {
    show(true);
    expect(screen.getByText(/the answer had no list/)).toBeInTheDocument();
  });

  it("draws the page again when asked and the cause is gone", () => {
    show();
    explode = false;
    fireEvent.click(screen.getByRole("button", { name: "Try the page again" }));
    expect(screen.getByText("the page is fine")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("draws a healthy page untouched", () => {
    explode = false;
    show();
    expect(screen.getByText("the page is fine")).toBeInTheDocument();
  });

  it("says the app was updated when a page file is missing, and offers to reload instead of a try-again that cannot work", () => {
    function Missing(): never { throw new TypeError("Failed to fetch dynamically imported module: http://127.0.0.1:58334/assets/RecordsPage-BwU4vUME.js"); }
    render(<DevModeProvider initial={false}><PageBoundary name="Records"><Missing /></PageBoundary></DevModeProvider>);
    expect(screen.getByRole("alert")).toHaveTextContent("The app was updated while this window was open.");
    expect(screen.getByRole("button", { name: "Reload the app" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try the page again" })).toBeNull();
  });
});
