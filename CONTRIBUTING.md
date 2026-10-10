# Contributing to local-llm-ctxgate-proxy

Thank you for your interest in contributing! This project is a local LLM
context gateway that optimizes KV-cache hit rates for vLLM by managing
session-scoped context windows, 4B memory workers, and a real-time
dashboard.

## Prerequisites

- Python 3.10+
- PostgreSQL 14+ (for the memory/knowledge store)
- vLLM (or any OpenAI-compatible LLM server)
- Optional: LM Studio with a 4B model for the memory worker

## Setup

```bash
# Clone
git clone https://github.com/PawelWos1987/local-llm-ctxgate-proxy.git
cd local-llm-ctxgate-proxy

# Create virtualenv
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure
cp config.example.yaml config.yaml
# Edit config.yaml with your vLLM model name and DB DSN

# Set environment variables (or use .env file)
export CTXGATE_DB_DSN="postgresql://postgres:your-password@127.0.0.1:5432/ctxproxy"

# Start PostgreSQL
docker compose up -d postgres

# Run migrations
make migrate

# Start the proxy
python proxy/app.py

# Start the dashboard (separate terminal)
python dashboard/dashboard.py

# Start the 4B memory worker (optional)
python worker/worker.py
```

## Testing

```bash
# Unit tests (fast, no external services needed)
make test-unit

# Full test suite (requires vLLM + PostgreSQL running)
make test

# Lint
make lint
```

## Project Structure

```
proxy/       # Core proxy (FastAPI, session management, KV-cache optimization)
worker/      # 4B memory worker (PostgreSQL, LM Studio)
dashboard/   # Real-time control room (FastAPI + HTML/JS)
schema/      # PostgreSQL migrations (001-008)
tests/       # Test suites (unit, integration, e2e, stress)
docs/        # Architecture documentation
```

## Code Style

- Follow PEP 8 (enforced by `ruff`)
- Type hints on all public functions
- Docstrings on all modules and public functions
- No hardcoded secrets, paths, or credentials
- Use environment variables for all configuration

## Pull Requests

1. Fork the repository and create a feature branch
2. Make your changes with tests
3. Ensure `make test-unit` and `make lint` pass
4. Submit a PR with a clear description of the change

## Reporting Bugs

Open an issue on GitHub with:
- Steps to reproduce
- Expected vs actual behavior
- Log output (proxy.log, dashboard.log)
- Configuration (config.yaml, environment variables)
