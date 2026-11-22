import { Pencil } from "lucide-react";
import { cn } from "../../lib/cn";

// A long piece of text (the project's system instructions, a conversation's extra instructions) shown collapsed to a few lines so it does not crowd the rest of the settings panel.
// Hovering (or focusing, for the keyboard) reveals a big edit button over the whole preview; the actual editing, and any caution about what the text controls, happens in the dialog it opens.

export function TextPreviewField({ label, value, placeholder, disabled, onEdit }: {
  label: string;
  value: string;
  placeholder: string;
  disabled?: boolean;
  onEdit: () => void;
}) {
  return (
    <div className="space-y-1">
      <h3 className="text-[0.7rem] font-semibold uppercase tracking-wider text-faint">{label}</h3>
      <div className="group relative border border-line bg-surface-2">
        <p className={cn("line-clamp-4 whitespace-pre-wrap break-words px-3 py-2 text-[0.78rem]", value ? "text-muted" : "italic text-faint")}>{value || placeholder}</p>
        <button type="button" aria-label={`Edit ${label.toLowerCase()}`} disabled={disabled} onClick={onEdit}
          className="absolute inset-0 flex items-center justify-center bg-surface/80 opacity-0 transition-opacity focus-visible:opacity-100 enabled:group-hover:opacity-100 disabled:pointer-events-none">
          <span aria-hidden className="chamfer-sm inline-grid h-11 w-11 place-items-center border border-line-strong bg-surface-2 text-fg shadow-pop"><Pencil size={20} /></span>
        </button>
      </div>
    </div>
  );
}
