<div class="cw-hero">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/cw-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="assets/brand/cw-logo-light.png">
    <img class="cw-hero__mark" src="assets/brand/cw-mark.png" alt="">
  </picture>
  <h1>Codex Workflow</h1>
  <p class="cw-hero__maker">by Queopius</p>
  <p class="cw-hero__statement">Build with autonomy. Advance with evidence.</p>
  <p class="cw-hero__description">
    A controlled workflow layer for AI-assisted software development.
  </p>
  <div class="cw-actions">
    <a class="cw-button cw-button--primary" href="getting-started/">Get started</a>
    <a class="cw-button cw-button--secondary" href="https://github.com/Queopius/cw">View on GitHub</a>
  </div>
  <ol class="cw-flow" aria-label="CW workflow lifecycle">
    <li>Plan</li>
    <li>Implement</li>
    <li>Validate</li>
    <li>Review</li>
    <li>Gate</li>
  </ol>
  <p class="cw-hero__description">
    <a href="https://github.com/Queopius/cw/releases/latest">
      <img alt="CW release"
           src="https://img.shields.io/github/v/release/Queopius/cw?display_name=tag&sort=semver">
    </a>
  </p>
  <p class="cw-invariant">No valid gate. No next phase.</p>
</div>

CW gives Codex bounded room to implement while keeping advancement under a
deterministic supervisor. Plans are explicit, implementation and review use
separate agents, validation runs before approval, and every next phase depends
on verified gate evidence.

New workflows also declare what evidence would prove the product-level goal.
After authorized phase work finishes, an independent completion review either
records final evidence or proposes the smallest coherent extension for explicit
human authorization.

## Quick start

Run these commands from a Git repository after [installing CW](getting-started.md#install-cw):

```bash title="Create and run a controlled workflow"
cw init
cw plan --goal "Implement subscription billing"
cw plan show
cw plan approve
cw
```

The final `cw` command starts or resumes the current phase. CW does not silently
run every remaining phase; use a bounded [`cw run`](batch-execution.md) request
when you intentionally want more than one gated phase.

## Choose your path

<div class="cw-paths">
  <a class="cw-card" href="getting-started/">
    <strong>Start with CW</strong>
    <span>Install CW, initialize a repository, approve a plan, and run your first phase.</span>
  </a>
  <a class="cw-card" href="workflow/">
    <strong>Understand CW</strong>
    <span>Learn the lifecycle, state transitions, independent review, and gate invariants.</span>
  </a>
  <a class="cw-card" href="live-execution/">
    <strong>Operate CW</strong>
    <span>Follow live execution, inspect runs, use bounded batches, and manage integrations.</span>
  </a>
  <a class="cw-card" href="troubleshooting/">
    <strong>Recover safely</strong>
    <span>Diagnose symptoms, understand error codes, and reconcile state without discarding evidence.</span>
  </a>
  <a class="cw-card" href="mcp-runtime/">
    <strong>Use governed MCP</strong>
    <span>Inspect CW or request one of four controlled actions without exposing shell or filesystem access.</span>
  </a>
</div>

## What makes progression trustworthy

| Control | What it guarantees |
| --- | --- |
| Explicit plan | Implementation cannot rewrite its own acceptance criteria. |
| Deterministic validation | Required commands and artifact checks run before semantic review. |
| Independent reviewer | A separate read-only Codex process evaluates current-phase evidence. |
| Verified gate | Artifact hashes and review evidence must validate before advancement. |
| Fail-closed state | Contradictory cached state and gate evidence stop normal execution. |

!!! tip "Find the command you need"
    Use the [CLI reference](cli-reference.md) for concise syntax, options, and
    examples validated against the source parser.

When every authorized phase has a valid contiguous gate chain, planned scope is
complete and no further implementer is launched. Contract-aware projects become
semantically `COMPLETED` only after valid completion evidence; legacy projects
retain their existing completion behavior.

> **All valid gates. No next phase.**

[CW product website](https://cwcli.dev) · [Source repository](https://github.com/Queopius/cw)
