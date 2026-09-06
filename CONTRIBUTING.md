# Contributing

Thank you for improving FreeModels Web2API.

## Boundaries

Contributions must preserve the project's local-first, responsible-use design.
Changes that add account rotation, proxy rotation, CAPTCHA bypass, browser
cookie extraction, credential discovery, rate-limit evasion, or public-relay
features will not be accepted.

## Development setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m unittest discover -s tests -v
```

## Pull requests

1. Keep changes focused and explain the interoperability need.
2. Add or update tests for behavior changes.
3. Avoid live upstream calls in automated tests; use deterministic SSE fixtures.
4. Update the README when configuration or behavior changes.
5. Confirm that no API keys, cookies, prompt data, or personal information are committed.

Contributions are licensed under the repository's MIT License.
