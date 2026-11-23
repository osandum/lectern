---
name: open-with-lectern
description: Use right after writing a standalone Markdown deliverable (a report, summary, plan, writeup, or similar .md file meant for the user to read) to disk, in ANY project — not just when asked to. Instead of just naming the file path, also open it in Lectern, the user's GTK Markdown viewer. Trigger whenever the next thing you'd otherwise do is say "I've saved this to <path>.md" or similar — and read it *before* composing such a file, since it says what Lectern renders (mermaid diagrams, local images, tables, footnotes). Do not trigger for incidental files (configs, code, data) or for .md files that are project source (docs, README, AGENTS.md) rather than a generated deliverable.
---

# Open with Lectern

The user reads generated Markdown deliverables with
[Lectern](https://github.com/osandum/lectern), a native GTK4/Libadwaita
Markdown viewer, rather than in the terminal or an editor. Whenever you
write a Markdown file that *is* the deliverable — a report, summary,
research writeup, plan, comparison, etc. — open it in Lectern as the
last step, instead of just telling the user where the file is.

## Steps

1. Finish writing the file as normal.
2. Check a viewer is plausible before trying to launch one:
   - `command -v lectern` — if missing, Lectern isn't installed here;
     skip launching and just report the path as usual (don't mention
     the skill or apologize for it).
   - A display is reachable — `$DISPLAY` or `$WAYLAND_DISPLAY` is set.
     Over a headless SSH session with neither, skip launching too.
3. Launch it detached, so it doesn't block and doesn't die when the
   turn ends:
   ```sh
   setsid lectern /absolute/path/to/file.md >/dev/null 2>&1 &
   disown
   ```
   Use the Bash tool's `run_in_background: true` as an alternative to
   `setsid`/`disown` if that's more convenient — either way, don't wait
   on it and don't treat a nonzero/unknown exit as an error worth
   surfacing.
4. Mention in your reply that you opened it — a short "opened in
   Lectern" alongside the path is enough, no need to explain the
   mechanism.

## Writing for Lectern

Lectern renders more than plain prose, so write the deliverable to use it:

- **Mermaid diagrams are drawn natively** — `flowchart`/`graph`,
  `sequenceDiagram`, `classDiagram` and `erDiagram`, including
  subgraphs, `classDef`/`style`/`linkStyle` and `click` links. Prefer a
  diagram to describing structure in prose. Other diagram types
  (gantt, pie, state, …) fall back to a highlighted code block, so
  avoid them. Edges are drawn at right angles like GitHub's, so keep
  each diagram small (roughly ≤ 12–15 nodes, ≤ ~4 side by side, short
  labels); several focused diagrams beat one crowded one.
- **Local images are shown**, resolved relative to the `.md` file, and
  linked images are clickable. Remote (`http(s)`) images are held back
  behind a banner until the reader allows them, so save a generated
  chart or screenshot next to the report and reference it by relative
  path, not by URL.
- **GFM tables, footnotes, task lists, strikethrough and bare-URL
  autolinks** all render; column widths are fitted to content, so wide
  comparison tables are fine.
- **Copy and print preserve formatting** — the clipboard carries HTML
  (diagrams as PNG) and Markdown, and PDF export keeps links clickable —
  so there's no need to also produce an HTML version for sharing.

## Non-goals

- Don't launch Lectern for files you edited that are part of a
  repo's existing docs (README.md, AGENTS.md, CHANGELOG.md, etc.) —
  only for a fresh document generated as this turn's output.
- Don't launch it more than once per file per turn, and don't re-open
  it on subsequent edits to the same file within the same
  conversation — once is enough, the user can switch back to it (and
  Lectern auto-reloads on change anyway).
- If the user is actively working *in* the lectern/mdview repo itself,
  use ordinary judgment — this skill is about generated deliverables in
  other settings, not about dogfooding every fixture file.
