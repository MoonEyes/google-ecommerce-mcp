# Minimal image: the server starts and answers MCP introspection without any Google credentials
# (every tool then returns "not_configured"). Pass the env variables and a token file to use it for real.
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
ENTRYPOINT ["google-ecommerce-mcp"]
