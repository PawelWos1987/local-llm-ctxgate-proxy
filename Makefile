.PHONY: test test-unit lint run worker db db-down db-logs clean

test:
	pytest tests/ -v

test-unit:
	pytest tests/ -v -k 'not e2e and not stress and not load'

lint:
	ruff check .
	mypy proxy/ worker/

run:
	python proxy/app.py

worker:
	python worker/worker.py

db:
	docker compose up -d postgres

db-down:
	docker compose down

db-logs:
	docker compose logs -f postgres

clean:
	rm -rf __pycache__ .mypy_cache .pytest_cache
	find . -name '*.pyc' -delete
