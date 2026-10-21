# syntax=docker/dockerfile:1

ARG PIXI_IMAGE=ghcr.io/prefix-dev/pixi:0.82.1-trixie-slim@sha256:f07e62caf7d6313297362028eef225897443ed841c8c0b43c8e3ddc032db43ac
ARG RUNTIME_IMAGE=gcr.io/distroless/base-debian13:nonroot@sha256:7e22204ffcba783587afba539fcc199cc8c91a7583b66299ac4dccdb5fed5f03

# --- build: resolve the locked prod environment and collect static files ---
FROM ${PIXI_IMAGE} AS build
WORKDIR /app
COPY pixi.toml pixi.lock ./
RUN pixi install --locked -e prod && pixi install --locked -e build
COPY scripts/tailwind.py ./scripts/tailwind.py
COPY src ./src
# Tailwind CSS (pinned, checksum verified binary) and compiled translations.
RUN pixi run -e build css && pixi run -e build compilemessages
# Throwaway values: collectstatic imports settings but touches neither DB nor secrets.
RUN PYTHONPATH=/app/src DJANGO_SETTINGS_MODULE=patchmanager.settings \
    DJANGO_SECRET_KEY=build-only-not-a-secret-0123456789-0123456789-0123456789 \
    DATABASE_URL=postgresql://build@localhost/build \
    PATCHMANAGER_TESTING=true \
    PATCHMANAGER_ENCRYPTION_KEYS=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA= \
    PATCHMANAGER_STATIC_ROOT=/app/staticfiles \
    /app/.pixi/envs/prod/bin/python -m django collectstatic --noinput \
    && mkdir -p /app/media/attachments

# --- dev: full dev environment, source is bind-mounted by compose.yaml ---
FROM ${PIXI_IMAGE} AS dev
WORKDIR /app
COPY pixi.toml pixi.lock ./
RUN pixi install --locked -e dev
ENV PATH=/app/.pixi/envs/dev/bin:$PATH \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=patchmanager.settings \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "django", "runserver", "0.0.0.0:8000"]

# --- prod: distroless, no shell, non-root ---
FROM ${RUNTIME_IMAGE} AS prod
ARG VERSION=0.0.0-dev
LABEL org.opencontainers.image.title="PatchManager" \
      org.opencontainers.image.description="Self-hosted network and rack documentation platform" \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.source="https://github.com/MalteKiefer/PatchManager" \
      org.opencontainers.image.version="${VERSION}"
WORKDIR /app
COPY --from=build /app/.pixi/envs/prod /app/.pixi/envs/prod
COPY --from=build /app/src /app/src
COPY --from=build /app/staticfiles /app/staticfiles
# Owned by the runtime user so a fresh named volume inherits writable permissions.
COPY --from=build --chown=65532:65532 /app/media /app/media
ENV PATH=/app/.pixi/envs/prod/bin \
    PYTHONPATH=/app/src \
    DJANGO_SETTINGS_MODULE=patchmanager.settings \
    PATCHMANAGER_STATIC_ROOT=/app/staticfiles \
    PATCHMANAGER_MEDIA_ROOT=/app/media \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROMETHEUS_MULTIPROC_DIR=/tmp/prometheus
USER 65532:65532
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["/app/.pixi/envs/prod/bin/python", "-c", "import sys, urllib.request; sys.exit(urllib.request.urlopen('http://localhost:8000/health/live', timeout=4).status != 200)"]
ENTRYPOINT ["/app/.pixi/envs/prod/bin/gunicorn", "--config", "/app/src/patchmanager/gunicorn_conf.py", "patchmanager.wsgi:application"]
