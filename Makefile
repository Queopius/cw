.PHONY: test install check docs-check demo demo-hero demo-check

test:
	python3 -m unittest discover -s tests -v

check:
	python3 -m compileall -q cw tests
	python3 -m unittest discover -s tests
	python3 scripts/validate_hero_demo.py

docs-check:
	python3 -m mkdocs build --strict

install:
	./install.sh

demo:
	python3 -m unittest tests.test_demo

demo-hero:
	python3 scripts/record_hero_demo.py

demo-check:
	python3 scripts/validate_hero_demo.py
