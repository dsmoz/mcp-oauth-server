FROM python:3.11-slim
WORKDIR /app
RUN pip install uv --no-cache-dir
COPY pyproject.toml uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o /tmp/requirements.txt && \
    uv pip install --system --no-cache-dir -r /tmp/requirements.txt
COPY src/ ./src/
COPY main.py ./
ENV PORT=8000
CMD ["python", "main.py"]
