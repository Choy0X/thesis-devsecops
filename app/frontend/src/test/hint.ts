import { act, screen } from "@testing-library/react";

/** The text of the tooltip an element shows when it takes the keyboard focus (the way a Hint is read without a pointer). */
export async function tipOf(el: HTMLElement): Promise<string> {
  await act(async () => { el.focus(); });
  return (await screen.findByRole("tooltip")).textContent ?? "";
}
