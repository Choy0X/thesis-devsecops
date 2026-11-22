import { Button } from "./ui";

export function TablePager({ offset, total, onOffset }: { offset: number; total: number; onOffset: (offset: number) => void }) {
  return <div className="mt-3 flex items-center gap-3 text-[0.78rem] text-muted" aria-label="Pagination">
    <Button variant="ghost" disabled={!offset} onClick={() => onOffset(Math.max(0, offset - 100))}>Previous</Button>
    <span>{total ? `${offset + 1}-${Math.min(offset + 100, total)} of ${total}` : "0 results"}</span>
    <Button variant="ghost" disabled={offset + 100 >= total} onClick={() => onOffset(offset + 100)}>Next</Button>
  </div>;
}
