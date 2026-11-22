import { useState } from "react";
import type { AgentPendingCall } from "../../lib/api";
import { Button, Textarea } from "../ui";
import { EditDiff } from "./AgentActivity";
import { callText, editOf } from "./agentSteps";

// Shown in place of the composer when a write or a command waits for the owner (pending_call, any tool but the two questions of Plan mode). It says what is asked and why, shows the call
// as the agent wrote it (an edit as what goes and what comes), and offers: Approve (this once), Allow always for this session or for the whole project (only when the call can be covered by
// a pattern, and the patterns are shown so what is being allowed is never a surprise), and Deny, with a note to the agent when there is something to say.

/** "always" is only ever offered for the dependency scan (its own dialog): a rule of the owner's for every project. */
export type Remember = "session" | "project" | "always";

export function ApprovalCard({ call, busy, onApprove, onDeny }: {
  call: AgentPendingCall;
  busy: boolean;
  onApprove: (remember?: Remember) => void;
  onDeny: (feedback?: string) => void;
}) {
  const [noting, setNoting] = useState(false);
  const [note, setNote] = useState("");
  const always = call.permission?.always ?? [];
  const edit = call.name === "edit_file" ? editOf(call.arguments) : null;
  return (
    <div role="alertdialog" aria-label="Approval needed" className="space-y-2 border border-warn-line bg-warn-tint p-3">
      <p className="text-[0.84rem] font-semibold">Approval needed: {call.name}</p>
      {call.permission?.reason && <p className="text-[0.78rem] text-muted">{call.permission.reason}</p>}
      {edit ? <EditDiff path={edit.path} old={edit.old} next={edit.new} />
        : <pre className="max-h-40 overflow-y-auto whitespace-pre-wrap break-all font-mono text-[0.74rem]">{call.name === "run_command" || call.name === "bash" ? callText(call.name, call.arguments) : call.arguments}</pre>}
      {always.length > 0 && (
        <p className="text-[0.76rem] text-muted">Allow always would cover: {always.map((p) => <code key={p} className="mr-1 bg-surface px-1 font-mono">{p}</code>)}</p>)}
      <div className="flex flex-wrap gap-2">
        <Button variant="primary" disabled={busy} onClick={() => onApprove()}>Approve</Button>
        {always.length > 0 && <Button variant="ghost" disabled={busy} onClick={() => onApprove("session")}>Allow for this session</Button>}
        {always.length > 0 && <Button variant="ghost" disabled={busy} onClick={() => onApprove("project")}>Allow for this project</Button>}
        <Button variant="ghost" disabled={busy} onClick={() => (noting ? onDeny(note.trim() || undefined) : onDeny())}>{noting ? "Send note and deny" : "Deny"}</Button>
        {!noting && <Button variant="ghost" disabled={busy} onClick={() => setNoting(true)}>Deny with a note</Button>}
      </div>
      {noting && <Textarea aria-label="What should the agent do instead?" value={note} rows={2} autoFocus disabled={busy} placeholder="What should it do instead?" onChange={(e) => setNote(e.target.value)} />}
    </div>
  );
}
