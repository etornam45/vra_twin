.PHONY: install test validate schemas e2e run-dashboard run-scada clean

VENV ?= venv
PYTHON := $(VENV)/bin/python

install:
	$(PYTHON) -m pip install -r requirements.txt

test:
	PYTHONPATH=. $(PYTHON) -m pytest contracts/tests/ gateway/tests/ backend/tests/ twin/tests/

validate:
	PYTHONPATH=. $(PYTHON) vra-validate.py validate sites/VLK-ISL-001

schemas:
	PYTHONPATH=. $(PYTHON) vra-validate.py export-schemas

e2e:
	PYTHONPATH=. $(PYTHON) -m pytest gateway/tests/test_e2e_pipeline.py

run-dashboard:
	PYTHONPATH=. $(PYTHON) -m backend.src.api.run

run-scada:
	PYTHONPATH=. $(PYTHON) tools/scada_sim/server.py

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
