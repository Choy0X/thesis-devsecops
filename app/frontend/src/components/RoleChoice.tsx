import { useState } from "react";
import { Button } from "./kit/Button";
import { RadioGroup } from "./kit";

// What an installed model will be set up as. The app works this out from the model (its name, its files, the architecture it declares), so the person is told and not asked: a sentence, and
// a small Change for the rare wrong guess. The two choices show only after Change, or when a choice was already made (then they stay, so it can be changed back).

export type Role = "chat" | "embedding";

export function RoleChoice({ role, detected, onChange, disabled }: { role: Role | null; detected: boolean | undefined; onChange: (role: Role) => void; disabled?: boolean }) {
  const [changing, setChanging] = useState(false);
  if (!role) return null;
  const chosen = !detected;                                // the person picked it (the plan was made for that role and says nothing about finding it), or the plan does not say: nothing is claimed
  if (!chosen && !changing) {
    return (
      <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-[0.8rem]">
        <p>
          <span className="font-medium">Will be set up as {role === "embedding" ? "an embedding model" : "a language model"}.</span>{" "}
          <span className="text-muted">Found from the name and the files of the model.</span>
        </p>
        {!disabled && <Button variant="ghost" size="sm" className="-ml-3" aria-label="Change what it is used as" onClick={() => setChanging(true)}>Change</Button>}   {/* -ml-3: its text lines up with the sentence when it wraps under it */}
      </div>
    );
  }
  return (
    <RadioGroup legend="Use it as" variant="segmented" value={role} onValueChange={(v) => onChange(v as Role)} disabled={disabled}
      options={[{ value: "chat", label: "Language model" }, { value: "embedding", label: "Embedding model" }]} />
  );
}
