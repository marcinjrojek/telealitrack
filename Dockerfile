FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app.py stock.py ./

ENV PATH="/app/.venv/bin:$PATH"
# make sure the installed playwright version has a matching browser
RUN playwright install --with-deps chromium

CMD ["python", "app.py"]
