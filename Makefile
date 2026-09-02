# Triveni - every target delegates to tasks.py so behaviour is identical on
# Linux, macOS and Windows. On Windows without GNU make, `make.cmd` provides
# the same entry point. See docs/adr/0002-task-runner.md.

PY := $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; elif [ -x .venv/Scripts/python.exe ]; then echo .venv/Scripts/python.exe; else echo python3; fi)

.DEFAULT_GOAL := help
.PHONY: help setup run test lint typecheck eval demo bench verify redteam data web secretscan clean all

help setup run test lint typecheck eval demo bench verify redteam data web secretscan clean all:
	@$(PY) tasks.py $@ $(ARGS)
