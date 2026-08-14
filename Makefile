.PHONY: test install check docs-check acceptance-local demo demo-hero demo-check

test:
	python3 -m unittest discover -s tests -v

check:
	python3 -m compileall -q cw tests
	python3 -m unittest discover -s tests
	python3 scripts/check_cli_docs.py
	python3 scripts/check_error_docs.py
	python3 scripts/check_doc_links.py
	python3 scripts/validate_hero_demo.py

docs-check:
	python3 scripts/check_cli_docs.py
	python3 scripts/check_error_docs.py
	python3 scripts/check_doc_links.py
	python3 -m mkdocs build --strict

acceptance-local: check docs-check
	python3 scripts/run_acceptance.py --output artifacts/compatibility-report.json

install:
	./install.sh

demo:
	python3 -m unittest tests.test_demo

demo-hero:
	python3 scripts/record_hero_demo.py

demo-check:
	python3 scripts/validate_hero_demo.py
