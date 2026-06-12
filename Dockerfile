FROM python:3.12-slim
WORKDIR /workspace
COPY pyproject.toml ./
RUN pip install --no-cache-dir fastapi uvicorn pydantic python-multipart
COPY . .
# Reserved API port is 18000 (ADR-0005). Container binds 0.0.0.0; restrict host
# exposure via the compose port publish (127.0.0.1) — see docker-compose.yml.
CMD ["uvicorn", "app.backend.main:app", "--host", "0.0.0.0", "--port", "18000"]
