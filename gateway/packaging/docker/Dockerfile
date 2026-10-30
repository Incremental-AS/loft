# Loft gateway container image (linux/amd64, linux/arm64).
#
#   docker buildx build --platform linux/amd64,linux/arm64 -f packaging/docker/Dockerfile gateway/
#
# Build context: the gateway/ folder. The thumbnail decoder runs as its own unprivileged
# user (loft-thumb), separate from the gateway (loft), with rlimits per file; there is
# no systemd sandbox in a container (see packaging/docker/README.md).
ARG PYTHON=3.12

FROM python:${PYTHON}-slim-bookworm AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
COPY requirements.lock /src/requirements.lock
RUN python -m venv /opt/loft/venv \
 && /opt/loft/venv/bin/pip install --require-hashes -r /src/requirements.lock
COPY pyproject.toml /src/
COPY loft_gateway /src/loft_gateway
RUN /opt/loft/venv/bin/pip install --no-deps /src
RUN /opt/loft/venv/bin/python -c "import loft_gateway.thumbworker, av, pillow_heif, pypdfium2"

FROM python:${PYTHON}-slim-bookworm
LABEL org.opencontainers.image.title="Loft gateway" \
      org.opencontainers.image.description="Serves your SMB shares to the Loft apps" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.url="https://loft.incremental.no" \
      org.opencontainers.image.vendor="Incremental AS"
RUN groupadd --system --gid 10001 loft \
 && useradd --system --uid 10001 --gid loft --home-dir /var/lib/loft --shell /usr/sbin/nologin loft \
 && groupadd --system --gid 10002 loft-thumb \
 && useradd --system --uid 10002 --gid loft-thumb --home-dir /nonexistent --no-create-home \
            --shell /usr/sbin/nologin loft-thumb \
 && mkdir -p /etc/loft /var/lib/loft /run/loft \
 && chown loft:loft /etc/loft /var/lib/loft /run/loft && chmod 0700 /etc/loft /var/lib/loft
COPY --from=build /opt/loft/venv /opt/loft/venv
ENV PATH=/opt/loft/venv/bin:$PATH PYTHONUNBUFFERED=1 LOFT_CONFIG=/etc/loft/gateway.toml
VOLUME ["/etc/loft", "/var/lib/loft"]
# 8443: direct listener (TLS, pinned key). 8080: public listener, for a tunnel only.
EXPOSE 8443 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).status==200 else 1)" || exit 1
ENTRYPOINT ["loft-container"]
