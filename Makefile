PY ?= .venv/bin/python

.PHONY: setup run seed test bench voices eval

setup:            ## create the virtualenv and install dependencies
	python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r requirements.txt
	@[ -f .env ] || cp .env.example .env
	$(MAKE) voices
	@echo "Now put a model API key in .env (GROQ_API_KEY is free), then: make run"

voices:           ## download the local text-to-speech voice (about 60 MB)
	@mkdir -p data/voices; [ -f data/voices/en_US-lessac-medium.onnx ] || $(PY) -m piper.download_voices --download-dir data/voices en_US-lessac-medium

run:              ## start core systems + assistant on http://127.0.0.1:8000
	$(PY) -m scripts.run

seed:             ## regenerate the demo data for today
	$(PY) -m mock_systems.seed

test:             ## unit and integration tests (no LLM calls)
	$(PY) -m pytest -q

bench:            ## latency benchmark (needs the app running and an API key)
	$(PY) -m scripts.bench

eval:             ## evaluation cases graded without a model (needs the app running and an API key)
	$(PY) -m scripts.evals
