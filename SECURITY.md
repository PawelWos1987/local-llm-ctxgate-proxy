# Security Policy

## Scope

local-llm-ctxgate-proxy is a **local, single-user** context proxy. It binds to 127.0.0.1 by default and is designed to run on the same machine as the AI agent and the inference backend.

**Security boundary:** localhost / single-user. local-llm-ctxgate-proxy is NOT designed for multi-tenant, network-exposed, or production multi-user deployment. Authentication is optional (set CTXGATE_API_KEY to require a bearer token on /v1/chat/completions; off by default). There is no TLS and no rate limiting by design — these are the responsibilities of the host environment.

## Supported Versions

| Version | Supported |
|---------|-----------|
| v0.1.x  | Yes (current) |

## Reporting a Vulnerability

Please report security issues via [GitHub Security Advisories](https://github.com/PawelWos1987/local-llm-ctxgate-proxy/security/advisories/new). Do **not** open a public issue for vulnerabilities.

We aim to acknowledge all valid reports within 72 hours.

## Threat Model

- **Network:** local-llm-ctxgate-proxy listens on 127.0.0.1 only. No external network exposure by default.
- **Data:** All data (conversation context, memories, events) stays in local PostgreSQL. No data leaves the machine.
- **Model endpoints:** local-llm-ctxgate-proxy communicates with local inference servers (vLLM, LM Studio) over localhost. No API keys are stored or transmitted to external services.
- **Dependencies:** See .github/dependabot.yml for automated dependency vulnerability scanning.
