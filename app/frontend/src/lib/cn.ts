import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Joins class names and resolves Tailwind conflicts (the later class wins), as the shadcn components do. */
export const cn = (...inputs: ClassValue[]) => twMerge(clsx(inputs));
