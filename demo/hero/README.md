# CW hero demo recorder

## Purpose

This directory holds the stable public event contract used by the future CW
landing-page hero. The official `hero-demo.json` is accepted only after a real,
disposable CW workflow reaches deterministic validation, independent review, a
verified approval gate, and canonical completion.

The website should consume only `event.type`, `event.text`, and the optional
`event.command`, `event.result`, and `event.actual_duration_ms` fields. Actual
durations are audit facts, not required playback delays; the site controls its
own accessible presentation timing.

## Generate

```bash
python scripts/record_hero_demo.py --dry-run
python scripts/record_hero_demo.py
```

For non-interactive maintainer use:

```bash
python scripts/record_hero_demo.py --yes
```

Requirements: a current installed CW build, authenticated Codex, network
connectivity, Python, and Git. `--cw-executable` may point to a disposable
installed wheel for release acceptance without replacing the user's managed CW.

## Validate offline

```bash
python scripts/validate_hero_demo.py
```

CI validates the committed artifact and never calls Codex.

## Safety

- the template is copied into a disposable Git repository and never mutated;
- the user's managed CW installation and global Codex/Git configuration are not changed;
- the previous recording is replaced atomically only after a real valid gate;
- private paths, ANSI, secrets, MCP noise, prompts, and model reasoning are excluded;
- a failed recording preserves the last-known-good artifact.

Future site maintainers should copy `demo/hero/hero-demo.json` and validate it
against `demo/hero/hero-demo.schema.json`. A synthetic test fixture must never
be presented as a real CW recording.
