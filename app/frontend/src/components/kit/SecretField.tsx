import { Eye, EyeOff, Trash2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { FormRow } from "./FormRow";
import { IconButton } from "./IconButton";
import { Spinner } from "./Spinner";
import { Input } from "./TextField";

// A key, a token or a password. Three things the old field got wrong: (1) nothing wraps the control and its buttons in a <label>, so a click can only do what it points at;
// (2) the Eye and the Remove are two real icon buttons side by side, each with a name and a tooltip, instead of a tiny underlined word above the field; (3) Remove is a
// request, not an action: `onRemove` is called and the caller asks "Remove the saved ...?" before anything is deleted.
// A saved value is never sent to the page with the settings, so with nothing typed the field is empty and the Eye would have nothing to show. `onReveal` is how the page asks for it,
// and only when the Eye is pressed: the value is held while it is shown, dropped when it is hidden or when the saved value changes, and never kept anywhere else. Typing into the shown
// value edits it, and the whole edited text is the new value; typing into the empty field replaces the saved one.

export type SecretFieldProps = {
  label: ReactNode;
  value: string;
  onChange: (value: string) => void;
  /** A value is stored (the page never gets it back with the settings). */
  saved?: boolean;
  /** The last characters of the stored value, to tell two of them apart. */
  savedHint?: string;
  /** Reads the stored value, for the Eye of a saved field that has nothing typed. */
  onReveal?: () => Promise<string>;
  /** The person pressed Remove. The caller asks for confirmation and deletes. */
  onRemove?: () => void;
  hint?: ReactNode;
  error?: ReactNode;
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  name?: string;
  rowClassName?: string;
};

const flush = "chamfer-none h-full w-9 self-stretch border-0 border-l border-line first:border-l-0";

export function SecretField({ label, value, onChange, saved, savedHint, onReveal, onRemove, hint, error, placeholder, disabled, id, name, rowClassName }: SecretFieldProps) {
  const [shown, setShown] = useState(false);
  const [revealed, setRevealed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);
  // The saved value changed or was removed: what was read is out of date.
  useEffect(() => { setRevealed(null); setShown(false); setFailure(""); }, [saved, savedHint]);
  const what = typeof label === "string" ? label : "value";
  const asks = !!saved && !!onReveal && value === "";

  const toggle = async () => {
    if (shown) { setShown(false); setRevealed(null); return; }
    setFailure("");
    if (!asks) { setShown(true); return; }
    setBusy(true);
    try {
      const got = await onReveal!();
      if (alive.current) { setRevealed(got); setShown(true); }
    } catch (e) {
      if (alive.current) setFailure(`The saved value could not be read: ${e instanceof Error ? e.message : String(e)}`);
    } finally { if (alive.current) setBusy(false); }
  };

  return (
    <FormRow label={label} hint={hint} error={error ?? (failure || undefined)} id={id} className={rowClassName}
      action={saved ? (
        <span className="caps text-faint" data-testid="secret-saved">saved{savedHint ? <span className="ml-1 font-mono normal-case tracking-normal text-ok">...{savedHint}</span> : null}</span>
      ) : undefined}>
      <Input name={name} type={shown ? "text" : "password"} value={value !== "" ? value : (shown ? (revealed ?? "") : "")} onChange={(e) => { setRevealed(null); onChange(e.target.value); }} disabled={disabled}
        placeholder={saved ? "******** (saved, type to replace)" : placeholder} autoComplete="off" spellCheck={false} inputClassName="font-mono"
        suffixClassName="grid-flow-col gap-0 p-0"
        suffix={(
          <>
            <IconButton label={shown ? `Hide ${what}` : `Show ${what}`} aria-pressed={shown} aria-busy={busy || undefined} size="sm" className={flush} disabled={disabled || busy} onClick={() => void toggle()}>
              {busy ? <Spinner size={15} /> : shown ? <EyeOff size={15} aria-hidden /> : <Eye size={15} aria-hidden />}
            </IconButton>
            {saved && onRemove && (
              <IconButton label={`Remove saved ${what}`} tone="danger" size="sm" className={flush} disabled={disabled} onClick={onRemove}>
                <Trash2 size={15} aria-hidden />
              </IconButton>
            )}
          </>
        )} />
    </FormRow>
  );
}
