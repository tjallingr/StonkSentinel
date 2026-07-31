.PHONY: install init collect run lint

install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt

init:
	PYTHONPATH=src .venv/bin/python -m finoverview.cli init

collect:
	PYTHONPATH=src .venv/bin/python -m finoverview.cli collect

run:
	PYTHONPATH=src .venv/bin/uvicorn finoverview.web.app:app --port 8080

lint:
	ruff check src

get-saxo-token:

