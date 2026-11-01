PYTHON ?= uv run --frozen --no-sync python
.PHONY: install install-inference check check-red lab-init flow-demo flow-fail real-prepare real-flow serve measure evidence
install:
	uv sync --frozen
install-inference:
	uv sync --frozen --extra inference
check:
	$(PYTHON) fast_checks.py
check-red:
	$(PYTHON) fast_checks.py --intentional-failure
lab-init:
	$(PYTHON) lab.py init
flow-demo:
	$(PYTHON) flow.py --workspace .lab --synthetic
flow-fail:
	$(PYTHON) flow.py --workspace .lab --synthetic --fail-after-prepare
real-prepare:
	$(PYTHON) prepare_real.py
real-flow:
	$(PYTHON) flow.py --workspace .lab-real
serve:
	$(PYTHON) service.py --workspace .lab-real --backend real --port 8091 --warmup-seconds 0 --debug
measure:
	$(PYTHON) study.py
evidence:
	$(PYTHON) report.py
