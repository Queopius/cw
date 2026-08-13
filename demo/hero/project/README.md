# Demo API

This is a tiny, dependency-free Python application used only by CW's disposable
hero-demo recorder.

The recording goal is to add a `GET /health` endpoint that returns HTTP 200 and
the JSON object `{"status": "ok"}`, together with automated tests. This is a
local, dependency-free demonstration: it is not deployed, security-sensitive,
or a public API compatibility change, and it does not require human approval.

Run the test suite with:

```bash
python3 -m unittest discover -v
```
