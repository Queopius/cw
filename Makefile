.PHONY: test install check docs-check plugin-check production-readiness-check acceptance-local demo demo-hero demo-check public-version-sync docs-sync-stable

RTD_PROJECT_SLUG ?= cw-codex-workflow
RTD_STABLE_VERSION ?= v$(shell tr -d '\n' < VERSION)

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
	python3 scripts/validate_plugin_candidate.py
	python3 scripts/validate_plugin_production_readiness.py
	python3 scripts/validate_remote_candidate.py
	python3 scripts/validate_staging_bootstrap.py
	python3 scripts/build_plugin_candidate.py --check

plugin-check:
	python3 scripts/validate_plugin_candidate.py
	python3 scripts/validate_plugin_production_readiness.py
	python3 scripts/build_plugin_candidate.py --check
	python3 -m unittest tests.test_plugin_candidate tests.test_plugin_installation tests.test_chatgpt_development tests.test_plugin_production_readiness

production-readiness-check: plugin-check

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
		--version $(RTD_STABLE_VERSION) \
		--trigger-build

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
