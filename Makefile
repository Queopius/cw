.PHONY: test install check demo

test:
	python3 -m unittest discover -s tests -v

check:
	python3 -m compileall -q cw tests
	python3 -m unittest discover -s tests

install:
	./install.sh

demo:
	python3 -m unittest tests.test_demo
