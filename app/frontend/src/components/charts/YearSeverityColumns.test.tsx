import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ByYear } from "../../lib/api";
import { YearSeverityColumns, labelledYears, niceStep } from "./YearSeverityColumns";

const row = (year: number, over: Partial<ByYear["years"][number]> = {}) => {
  const r = { year, unrated: 0, low: 0, medium: 0, high: 0, critical: 0, ...over };
  return { ...r, total: r.unrated + r.low + r.medium + r.high + r.critical };
};
const data = (years: ByYear["years"], over: Partial<ByYear> = {}): ByYear => ({ total: years.reduce((s, y) => s + y.total, 0), dated: years.reduce((s, y) => s + y.total, 0), undated: 0, unrated: 0, years, ...over });
const sample = data([row(2019, { medium: 2 }), row(2020), row(2021, { high: 1, critical: 1, unrated: 1 })]);

describe("niceStep and labelledYears", () => {
  it("rounds the y axis to 1, 2, 2.5 or 5 times a power of ten", () => {
    expect(niceStep(0)).toBe(1);
    expect(niceStep(7)).toBe(2);
    expect(niceStep(100)).toBe(25);
    expect(niceStep(4000)).toBe(1000);
    expect(niceStep(3187)).toBe(1000);
  });
  it("labels every year when there are few, and every k-th plus the last when there are many", () => {
    const few = [2019, 2020, 2021];
    expect([...labelledYears(few)]).toEqual(few);
    const many = Array.from({ length: 40 }, (_, i) => 1990 + i);
    const shown = [...labelledYears(many)];
    expect(shown[0]).toBe(1990);
    expect(shown.length).toBeLessThanOrEqual(14);
    expect(shown.every((y, i) => i === 0 || y > shown[i - 1])).toBe(true);
  });
});

describe("YearSeverityColumns", () => {
  it("draws one column per year, with the counts in words for a screen reader", () => {
    render(<YearSeverityColumns data={sample} />);
    const chart = screen.getByRole("group", { name: /5 dated advisories from 2019 to 2021/ });
    const columns = within(chart).getAllByRole("img");
    expect(columns).toHaveLength(3);
    expect(columns[0]).toHaveAccessibleName("2019: 2 advisories. Medium 2.");
    expect(columns[1]).toHaveAccessibleName("2020: 0 advisories. none.");
    expect(columns[2]).toHaveAccessibleName("2021: 3 advisories. Critical 1, High 1, No severity word 1.");
  });

  it("is reached with the keyboard and reads the counts of the column that has focus", () => {
    render(<YearSeverityColumns data={sample} />);
    expect(screen.getByRole("status")).toHaveTextContent("Hover a column");
    const col = screen.getAllByRole("img")[2];
    expect(col).toHaveAttribute("tabindex", "0");
    fireEvent.focus(col);
    expect(screen.getByRole("status")).toHaveTextContent("2021: 3 advisories. Critical 1, High 1, No severity word 1.");
    fireEvent.blur(col);
    expect(screen.getByRole("status")).toHaveTextContent("Hover a column");
    fireEvent.mouseEnter(screen.getAllByRole("img")[0]);
    expect(screen.getByRole("status")).toHaveTextContent("2019: 2 advisories. Medium 2.");
  });

  it("gives each band a texture and a word in the legend, so colour is not the only carrier", () => {
    const { container } = render(<YearSeverityColumns data={sample} />);
    const legend = screen.getByRole("list", { name: "Legend" });
    expect(within(legend).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["Critical", "High", "Medium", "Low", "No severity word"]);
    expect(container.querySelector("pattern#ysc-diagonal")).not.toBeNull();
    expect(container.querySelector("pattern#ysc-cross")).not.toBeNull();
    expect(container.querySelector("pattern#ysc-dots")).not.toBeNull();
  });

  it("puts the same numbers in a table, newest year first, one click away", () => {
    render(<YearSeverityColumns data={sample} />);
    fireEvent.click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows[0]).toHaveTextContent("YearCriticalHighMediumLowNo severity wordAll");
    expect(rows[1]).toHaveTextContent("2021110013");
    expect(rows[3]).toHaveTextContent("2019002002");
  });

  it("says so when no vulnerability has a date, and how many were left out for lacking one", () => {
    const { rerender } = render(<YearSeverityColumns data={data([])} />);
    expect(screen.getByText(/No vulnerability in the store has a publication date yet/)).toBeInTheDocument();
    rerender(<YearSeverityColumns data={data([row(2024, { high: 3 })], { undated: 4 })} />);
    expect(screen.getByText(/4 advisories have no publication date and are not in a column/)).toBeInTheDocument();
  });

  it("stays quick with a long history", () => {
    const years = Array.from({ length: 56 }, (_, i) => row(1970 + i, { low: i, medium: 2 * i, high: i, critical: i % 7 }));
    const started = performance.now();
    render(<YearSeverityColumns data={data(years)} />);
    expect(performance.now() - started).toBeLessThan(800);
    expect(screen.getAllByRole("img")).toHaveLength(56);
  });
});
