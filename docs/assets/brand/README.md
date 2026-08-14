# CW Brand Assets

## Product

```text
CW
Codex Workflow
by Queopius
```

## Official digital identity

- Website: [cwcli.dev](https://cwcli.dev)
- Documentation: [docs.cwcli.dev](https://docs.cwcli.dev)
- Source: [github.com/Queopius/cw](https://github.com/Queopius/cw)

## Canonical source

File:

```text
cw-logo-original.png
```

SHA-256:

```text
24e302971f8c47a716de7b4c541866d8ea960f295beb6882b5377ada9266ac27
```

Status: **Do not modify.** This owner-supplied RGBA PNG is the archival source
for the official CW monogram. Derived assets must preserve its C and W geometry,
proportions, cuts, angles, spacing, overlap, gradient direction, and silhouette.

## Primary mark

`cw-mark.png` is the canonical general-purpose monogram. It contains no text or
external effects and retains transparent safe space around the official mark.

The background-oriented variant names describe the background they are for:

- `cw-logo-dark.png` — for dark backgrounds such as `#080814`, `#0B0B12`, and
  `#111827`;
- `cw-logo-light.png` — for light backgrounds such as `#FFFFFF`, `#F8FAFC`, and
  `#F1F5F9`.

Both variants use exactly the primary mark's alpha mask. Only RGB luminance is
adjusted to maintain contrast. `cw-mark-32.png` and `cw-mark-64.png` are
deterministic, centered icon derivatives.

## Product lockup

Preferred presentation uses the monogram with live, accessible text rather than
baking product text into an image:

```text
[mark] Codex Workflow
       by Queopius
```

Do not add another `CW` beside the monogram unnecessarily.

## Usage rules

Do not:

- alter or recreate the geometry;
- stretch, compress, rotate, or crop into the mark;
- recolor outside the approved general, dark-background, and light-background
  variants;
- add outlines, shadows, glow, bevel, 3D, or other effects;
- add an external circle or check mark;
- bake `CW`, `Codex Workflow`, or `by Queopius` into a new logo PNG.

## Reproducible derivatives

Pillow is required only for development-time asset generation and is not a CW
runtime dependency:

```bash
python -m pip install Pillow
python scripts/build_brand_assets.py
python scripts/build_brand_assets.py --check
```

The generator verifies the canonical source SHA-256 before writing derivatives
and never overwrites the original.

## Product statements

```text
Build with autonomy.
Advance with evidence.
```

```text
No valid gate. No next phase.
```

## Social and future web use

`cw-og.png` is the pre-existing social preview migrated unchanged from the
owner-provided brand folder. It is not a source for the monogram derivatives.
Future approved social artwork should use the `cw-og.png` name.

Maintainers of the separate future landing-page project should copy only
release-approved assets from these canonical paths:

```text
docs/assets/brand/cw-mark.png
docs/assets/brand/cw-logo-dark.png
docs/assets/brand/cw-logo-light.png
```

The landing page must not recreate or reinterpret the CW monogram.

## Legal attribution

Repository attribution, reproduced from `NOTICE`:

```text
CW by Queopius — Codex Workflow
Copyright 2026 Fantomid LLC

This product is licensed under the Apache License, Version 2.0.
```

See the repository `LICENSE`, `NOTICE`, and `pyproject.toml` for canonical legal
and package metadata.
