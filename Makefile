.PHONY: test test-unit lint run worker db db-down db-logs clean install-systemd ensure-proxy restart-proxy logs-proxy

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

install-systemd:
	sudo cp deploy/ctxgate-proxy.service /etc/systemd/system/
	sudo cp deploy/ctxgate-worker.service /etc/systemd/system/
	sudo cp deploy/ctxgate-dashboard.service /etc/systemd/system/
	sudo systemctl daemon-reload
	sudo systemctl enable --now ctxgate-proxy ctxgate-worker ctxgate-dashboard
	@echo "installed and started"

ensure-proxy:
	@systemctl is-active --quiet ctxgate-proxy || { \
		echo "ERROR: ctxgate-proxy is not running under systemd."; \
		echo "Do not start it manually. Run: sudo systemctl start ctxgate-proxy"; \
		exit 1; \
	}
	@echo "ctxgate-proxy is active under systemd"

restart-proxy:
	sudo systemctl restart ctxgate-proxy

logs-proxy:
	journalctl -u ctxgate-proxy -f

