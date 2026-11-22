import { act, fireEvent, screen } from "@testing-library/react";
import userEvent, { type UserEvent } from "@testing-library/user-event";

/** Opens a kit select and picks an option by its name, the way a person does: `await choose(user, screen.getByRole("combobox", { name: "Engine" }), "External")`. */
export async function choose(user: UserEvent, trigger: HTMLElement, option: string | RegExp): Promise<void> {
  await user.click(trigger);
  await user.click(await screen.findByRole("option", { name: option }));
}

/** The same without user-event and without waiting, for a test that runs on fake timers (`vi.useFakeTimers()`), where user-event and `findBy...` wait for ever: Enter on the trigger
 *  opens the list and a click on the option picks it (the way Radix takes a touch). */
export function chooseNow(trigger: HTMLElement, option: string | RegExp): void {
  act(() => { fireEvent.keyDown(trigger, { key: "Enter" }); });
  act(() => { fireEvent.click(screen.getByRole("option", { name: option })); });
}

/** Picks an option of the kit select named `label` without a user made by the test: `await pickOption("State", "trained")`. */
export async function pickOption(label: string, option: string | RegExp): Promise<void> {
  await choose(userEvent.setup(), screen.getByRole("combobox", { name: label }), option);
}
