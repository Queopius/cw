# Demo utility

This is a tiny, dependency-free Python utility used only by CW's disposable
hero-demo recorder.

The recording goal is to add a deterministic `greet(name)` function that returns
`Hello, {name}!`, together with automated tests. This local utility is not
deployed and has no external services or dependencies.

Run the test suite with:

```bash
python3 -m unittest discover -v
```
