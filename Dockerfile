FROM python:3.12-slim
WORKDIR /workspace
COPY pyproject.toml ./
RUN pip install --no-cache-dir fastapi uvicorn pydantic python-multipart
COPY . .
CMD ["uvicorn", "app.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
