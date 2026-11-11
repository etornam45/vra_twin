.PHONY: test validate schemas e2e run-scada clean

test:
	PYTHONPATH=. .venv/bin/pytest contracts/tests/ gateway/tests/

validate:
	.venv/bin/python3 vra-validate.py validate sites/VLK-ISL-001

schemas:
	.venv/bin/python3 vra-validate.py export-schemas

e2e:
	PYTHONPATH=. .venv/bin/pytest gateway/tests/test_e2e_pipeline.py

run-scada:
	.venv/bin/python3 tools/scada_sim/server.py

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
