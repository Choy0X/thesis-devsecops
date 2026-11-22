import { describe, expect, it } from "vitest";
import { gigabytes } from "./fitWords";

const GB = 1024 ** 3;
const MB = 1024 ** 2;

describe("gigabytes", () => {
  it("writes a size in gigabytes with one decimal, and none from 10 GB on", () => {
    expect([gigabytes(4 * GB), gigabytes(0.8 * GB), gigabytes(10 * GB), gigabytes(14.2 * GB), gigabytes(120 * GB)]).toEqual(["4.0 GB", "0.8 GB", "10 GB", "14 GB", "120 GB"]);
  });

  it("writes a size under a tenth of a gigabyte in the unit that fits, so a small model is not 0.0 GB", () => {
    expect([gigabytes(6.2 * MB), gigabytes(740 * 1024), gigabytes(500), gigabytes(0.05 * GB)]).toEqual(["6.2 MB", "740 kB", "500 B", "51.2 MB"]);
  });

  it("keeps a tenth of a gigabyte and a zero as they were", () => {
    expect(gigabytes(0.1 * GB)).toBe("0.1 GB");
    expect(gigabytes(0)).toBe("0.0 GB");
  });
});
