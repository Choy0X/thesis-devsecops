import { FinetuneRuns } from "./FinetuneRuns";

// The fine-tuning page is the evidence-based run flow. The earlier classification, detection and patch
// notebooks, the CSV import and the two-run comparison were removed; Compare in Train replaces that idea.
export function FineTuning() {
  return (
    <div className="space-y-5">
      <FinetuneRuns />
    </div>
  );
}
