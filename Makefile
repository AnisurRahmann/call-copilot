.PHONY: install test run run-verbose list status logs diagnose clean help

PYTHON := .venv/bin/python
PIP    := .venv/bin/pip
REC    := .venv/bin/rec

help:
	@echo "rec — meeting recorder"
	@echo "  make install       create venv + install dev deps"
	@echo "  make test          run unit tests"
	@echo "  make run           rec start"
	@echo "  make run-verbose   rec -v start (INFO logs on stderr)"
	@echo "  make list          rec list"
	@echo "  make status        rec status"
	@echo "  make logs          tail -f the global log"
	@echo "  make diagnose      rec diagnose <latest> --stdout (bundle for AI debugging)"
	@echo "  make clean         remove build artifacts"

install: .venv
	$(PIP) install -e ".[dev]"

.venv:
	python3.11 -m venv .venv
	$(PIP) install --upgrade pip

test:
	$(PYTHON) -m pytest tests -v

run:
	$(REC) start

run-verbose:
	$(REC) -v start

list:
	$(REC) list

status:
	$(REC) status

logs:
	@echo "Tailing ~/.local/share/rec/logs/rec.log (Ctrl+C to stop)"
	@tail -f ~/.local/share/rec/logs/rec.log

diagnose:
	@echo "Bundling debug info for the most recent session..."
	@latest=$$(ls -1 ~/.local/share/rec/sessions/ 2>/dev/null | tail -1); \
	if [ -z "$$latest" ]; then echo "No sessions found."; exit 1; fi; \
	$(REC) diagnose "$$latest" --stdout

clean:
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache
	find . -type d -name __pycache__ -exec rm -rf {} +
