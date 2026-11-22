import { describe, expect, it } from "vitest";
import { extensionOf, languageFor, languageName } from "./languages";

describe("which grammar a file gets", () => {
  it("reads the extension of a name, and none from a dotfile", () => {
    expect(extensionOf("src/App.TSX")).toBe("tsx");
    expect(extensionOf("C:\\code\\setup.py")).toBe("py");
    expect(extensionOf(".gitignore")).toBe("");
    expect(extensionOf("Makefile")).toBe("");
    expect(extensionOf("archive.tar.gz")).toBe("gz");
  });

  it("names the language of what it knows and nothing for the rest", () => {
    expect(languageName("app/client.tsx")).toBe("tsx");
    expect(languageName("a.mjs")).toBe("javascript");
    expect(languageName("Dockerfile")).toBe("dockerfile");
    expect(languageName("Dockerfile.dev")).toBe("dockerfile");
    expect(languageName("Makefile")).toBeNull();
    expect(languageName("notes.txt")).toBeNull();
    expect(languageName(".gitignore")).toBeNull();
  });

  it("loads a grammar for every language it names (a wrong import or export name fails here, not in front of the owner)", async () => {
    const files = ["a.ts", "a.tsx", "a.js", "a.jsx", "a.py", "a.json", "a.css", "a.html", "a.md", "a.sql", "a.yaml", "a.rs", "a.go", "a.java", "a.cpp", "a.php", "a.sh", "a.toml", "Dockerfile", "a.xml", "a.ps1", "a.rb", "a.kt", "a.swift"];
    for (const f of files) expect(await languageFor(f), f).not.toBeNull();
    expect(await languageFor("notes.txt")).toBeNull();
  });
});
