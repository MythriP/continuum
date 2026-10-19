.PHONY: install lint test demo data docker docker-run clean

install:        ## install the package and dev tools into the active environment
	pip install -e ".[dev]"

lint:           ## static checks
	ruff check src tests spikes

test:           ## offline test suite (no API key needed)
	pytest

demo:           ## one simulated day, offline, with the scripted investigator
	reconcile init
	reconcile work --oracle
	reconcile board

data:           ## regenerate the synthetic dataset (deterministic)
	python -m reconcile.data.generate

docker:         ## build the container image
	docker build -t continuum-reconcile:0.1.0 .

docker-run:     ## show the board from the container
	docker compose -f deploy/compose.yaml run --rm reconcile board

clean:          ## remove local runtime state (case store, sessions); keeps the replay cache
	rm -f var/reconcile.db* var/sessions.db*
