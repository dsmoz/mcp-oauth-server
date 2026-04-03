FROM python:3.11-slim
WORKDIR /app
RUN pip install uv --no-cache-dir
COPY pyproject.toml ./
RUN uv pip install --system -e . --no-cache-dir 2>/dev/null || uv pip install --system fastapi uvicorn supabase bcrypt python-dotenv jinja2 python-multipart structlog httpx pydantic pydantic-settings --no-cache-dir
COPY src/ ./src/
COPY main.py ./
ENV PORT=8000
CMD ["python", "main.py"]
