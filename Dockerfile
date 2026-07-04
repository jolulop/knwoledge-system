FROM python:3.12-slim
WORKDIR /workspace
COPY pyproject.toml ./
RUN pip install --no-cache-dir fastapi uvicorn pydantic python-multipart
COPY . .
# Reserved API port is 18000 (ADR-0005). The default CMD routes through the blessed entrypoint so
# assert_safe_bind (ADR-0009) governs the bind: a bare `docker run` binds loopback INSIDE the
# container — unreachable from the host, fail-closed, never silently exposed. Reachability is the
# compose exception only: it sets APP_HOST=0.0.0.0 + KS_ALLOW_INSECURE_BIND=1 and publishes the
# port on host loopback — see docker-compose.yml.
CMD ["python", "-m", "app.backend"]
