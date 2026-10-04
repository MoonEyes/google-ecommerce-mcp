# Contributing

Thanks for helping. Issues and pull requests are welcome, in English or French.

## Ground rules

1. **Read-only stays read-only.** A tool must never call an endpoint that creates, updates, publishes, submits or deletes. If a new tool needs a query `POST`, add its URL suffix to `allowed_posts` in `tests/test_server.py::test_every_call_is_read_only` and explain why in the PR.
2. **Errors are results.** Use `_call` and `@_guard` so failures come back as `{"error": ...}` objects.
3. **No hard-coded accounts.** Every id comes from an environment variable read in `config.py`.
4. **Keep answers small.** Flatten Google responses to what a model needs and cap row counts.

## Development

```bash
git clone https://github.com/MoonEyes/google-ecommerce-mcp
cd google-ecommerce-mcp
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pytest
```

Tests run offline against a fake HTTP session; no Google account is needed. Every new tool needs at least one test, and `test_tools_are_registered_with_real_signatures` must be updated with the new tool count.

To try a change against real data, run `google-ecommerce-mcp setup` with your own OAuth client, then:

```bash
npx @modelcontextprotocol/inspector google-ecommerce-mcp
```

## Adding a tool

1. Write the function in `server.py` under the right section, decorated with `@mcp.tool()` then `@_guard`.
2. Its docstring is what the model reads: say what it answers, the parameter formats and one example value.
3. Document it in `docs/TOOLS.md` and the table in `README.md`.
4. Add a line to `CHANGELOG.md`.

## Diagrams

Diagram sources are JSON files in `docs/diagrams/`, rendered with Archify (see `docs/ARCHITECTURE.md`). Update the JSON, rerender, and commit both the JSON, the HTML and the PNG.
