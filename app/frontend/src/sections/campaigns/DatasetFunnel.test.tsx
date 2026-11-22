import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { DatasetFunnel, funnelRows } from "./DatasetFunnel";

const detail = {
  funnel: [{ step: "usable pairs of mined records", rows: 85 }, { step: "quarantined commits excluded", rows: 78 }, { step: "test files excluded", rows: 56 }, { step: "at most 5 pairs per CVE", rows: 25 }],
  duplicates_removed: 3, leak_guard_removed: 0, unscored: 2, warnings: ["only 22 train rows - too few to fine-tune well"],
};

describe("where the rows of a draft dataset went", () => {
  it("adds the rows the split removes after the filters, and nothing when it removed none", () => {
    expect(funnelRows(detail).map((r) => r.rows)).toEqual([85, 78, 56, 25, 22]);
    expect(funnelRows({ funnel: [{ step: "a", rows: 5 }] })).toEqual([{ step: "a", rows: 5 }]);
    expect(funnelRows({})).toEqual([]);
  });

  it("lists every filter with the rows it left and the rows it dropped", () => {
    render(<DatasetFunnel detail={detail} />);
    const table = screen.getByRole("table", { name: "Where the rows of the draft dataset went" });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(5);
    expect(rows[0]).toHaveTextContent("usable pairs of mined records");
    expect(rows[0]).toHaveTextContent("85");
    expect(rows[0]).toHaveTextContent("start");
    expect(rows[1]).toHaveTextContent("quarantined commits excluded");
    expect(rows[1]).toHaveTextContent("78");
    expect(rows[1]).toHaveTextContent("-7");
    expect(rows[3]).toHaveTextContent("-31");
    expect(rows[4]).toHaveTextContent("exact copies");
    expect(rows[4]).toHaveTextContent("22");
    expect(rows[4]).toHaveTextContent("-3");
  });

  it("says how many rows have no severity and shows the warnings of the build", () => {
    render(<DatasetFunnel detail={detail} />);
    expect(screen.getByText("2 rows have no severity score.")).toBeInTheDocument();
    expect(screen.getByText("only 22 train rows - too few to fine-tune well")).toBeInTheDocument();
  });

  it("draws nothing for a build that reported no funnel", () => {
    const { container } = render(<DatasetFunnel detail={{}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
