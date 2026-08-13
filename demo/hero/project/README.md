# Demo API

This is a tiny, dependency-free Python application used only by CW's disposable
hero-demo recorder.

The recording goal is to add a `GET /health` endpoint that returns HTTP 200 and
the JSON object `{"status": "ok"}`, together with automated tests.

Run the test suite with:

```bash
python -m unittest discover -v
```
