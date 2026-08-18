.PHONY: test install check docs-check acceptance-local demo demo-hero demo-check public-version-sync docs-sync-stable

RTD_PROJECT_SLUG ?= cw-codex-workflow
RTD_STABLE_REF ?= prod

test:
	python3 -m unittest discover -s tests -v

check:
	python3 -m compileall -q cw tests
	python3 -m unittest discover -s tests
	python3 scripts/check_cli_docs.py
	python3 scripts/check_error_docs.py
	python3 scripts/check_doc_links.py
	python3 scripts/check_docs_policy.py
	python3 scripts/check_public_version.py
	python3 scripts/validate_hero_demo.py

docs-check:
	python3 scripts/check_cli_docs.py
	python3 scripts/check_error_docs.py
	python3 scripts/check_doc_links.py
	python3 scripts/check_docs_policy.py
	python3 scripts/check_public_version.py
	python3 -m mkdocs build --strict

docs-sync-stable:
	python3 scripts/sync_readthedocs_stable.py \
		--project $(RTD_PROJECT_SLUG) \
		--alias stable \
		--ref $(RTD_STABLE_REF)

public-version-sync:
	python3 scripts/sync_public_version.py
	python3 scripts/check_public_version.py

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
