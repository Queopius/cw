# Error reference

CW should use structured error codes and concise normal-mode summaries.

Examples observed during development include:

- `PROTECTED_PATH_MODIFIED`
- `STATE_INCONSISTENT`
- planner/reviewer transport or configuration errors

## Error handling philosophy

- deterministic configuration errors should not be presented as blindly retryable;
- infrastructure failures must not consume semantic review attempts;
- optional MCP diagnostics must not override a successful agent result;
- raw diagnostic detail belongs in `cw error` / verbose diagnostics.

This page should ultimately be generated or validated against the canonical error enum in source.
