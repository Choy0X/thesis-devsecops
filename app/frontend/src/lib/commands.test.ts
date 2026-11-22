import { describe, expect, it } from "vitest";
import { COMMANDS, matchCommands, parseCommand } from "./commands";

describe("parseCommand", () => {
  it("recognizes every known command, case-insensitively, with no argument", () => {
    for (const c of COMMANDS) {
      expect(parseCommand(`/${c.name}`)).toEqual({ name: c.name, arg: "" });
      expect(parseCommand(`/${c.name.toUpperCase()}`)).toEqual({ name: c.name, arg: "" });
    }
  });

  it("extracts the words after the name as the argument, trimmed", () => {
    expect(parseCommand("/resume  the auth question  ")).toEqual({ name: "resume", arg: "the auth question" });
    expect(parseCommand("/plan how should I fix this?")).toEqual({ name: "plan", arg: "how should I fix this?" });
  });

  it("is not a command when the leading word does not match a known name, so it is sent as an ordinary question", () => {
    expect(parseCommand("/nowhere near a command")).toBeNull();
    expect(parseCommand("/etc/passwd is readable, is that bad?")).toBeNull();
  });

  it("is not a command without a leading slash, or on an empty line", () => {
    expect(parseCommand("status")).toBeNull();
    expect(parseCommand("")).toBeNull();
    expect(parseCommand("   ")).toBeNull();
  });

  it("trims the line first, so leading space before the slash still counts", () => {
    expect(parseCommand("  /status  ")).toEqual({ name: "status", arg: "" });
  });
});

describe("matchCommands", () => {
  it("lists every command for an empty prefix", () => {
    expect(matchCommands("")).toHaveLength(COMMANDS.length);
  });

  it("narrows to the commands whose name starts with the prefix, case-insensitively", () => {
    expect(matchCommands("co").map((c) => c.name)).toEqual(["compact", "context"]);
    expect(matchCommands("CO").map((c) => c.name)).toEqual(["compact", "context"]);
  });

  it("is empty for a prefix no command starts with", () => {
    expect(matchCommands("zzz")).toEqual([]);
  });
});
