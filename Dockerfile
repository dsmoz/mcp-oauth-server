FROM python:3.11-slim
WORKDIR /app
RUN pip install uv --no-cache-dir
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml --no-cache-dir
COPY src/ ./src/
COPY main.py ./
ENV PORT=8000
CMD ["python", "main.py"]
