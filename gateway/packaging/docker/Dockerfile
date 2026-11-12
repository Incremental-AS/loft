# Loft gateway container image (linux/amd64, linux/arm64).
#
#   docker buildx build --platform linux/amd64,linux/arm64 -f packaging/docker/Dockerfile gateway/
#
# The licenses label is the SPDX expression of everything the image holds (our code, the Python
# packages, the libraries their wheels bundle, the Debian base); the release check
# (packaging/third_party/image-check.sh) refuses an image whose label is not what it computes.
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
RUN /opt/loft/venv/bin/python -c "import loft_gateway.thumbworker, loft_gateway.search, av, pillow_heif, pypdfium2, snowballstemmer"

FROM python:${PYTHON}-slim-bookworm
LABEL org.opencontainers.image.title="Loft gateway" \
      org.opencontainers.image.description="Serves your SMB shares to the Loft apps. Source of its GPL/LGPL parts: the *-sources.tar files on the GitHub release of this version, https://github.com/Incremental-AS/loft/releases" \
      org.opencontainers.image.licenses="0BSD AND Apache-2.0 AND (Apache-2.0 OR BSD-3-Clause) AND Artistic-1.0-Perl AND BSD-2-Clause AND BSD-2-Clause-Patent AND BSD-3-Clause AND (BSD-3-Clause OR GPL-2.0-only) AND BSD-3-Clause-Clear AND BSD-4-Clause AND FTL AND (FTL OR GPL-2.0-or-later) AND GPL-1.0-or-later AND GPL-2.0-only AND GPL-2.0-or-later AND GPL-3.0-only AND GPL-3.0-or-later AND ICU AND IJG AND ISC AND LGPL-2.0-only AND LGPL-2.0-or-later AND LGPL-2.1-only AND LGPL-2.1-or-later AND LGPL-3.0-only AND LGPL-3.0-or-later AND (LGPL-3.0-or-later OR GPL-2.0-or-later) AND libpng-2.0 AND libtiff AND MIT AND (MIT OR Apache-2.0) AND MIT-0 AND MIT-CMU AND MIT-open-group AND MPL-2.0 AND PSF-2.0 AND X11 AND Zlib" \
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
# Third-party notices and SBOM inside the image: /usr/share/doc/loft-gateway/third-party/
# (sbom.spdx.json, THIRD_PARTY_NOTICES.md, licenses/). The build stops when a library a wheel
# bundles is not explained by packaging/third_party/components.toml. The sources of the
# GPL/LGPL parts are published with each release (packaging/third_party/README.md).
COPY packaging/third_party/thirdparty.py packaging/third_party/components.toml /tmp/third_party/
COPY packaging/third_party/notices /tmp/third_party/notices
RUN /usr/local/bin/python3 -I /tmp/third_party/thirdparty.py collect / /usr/share/doc/loft-gateway/third-party \
 && rm -rf /tmp/third_party
ENV PATH=/opt/loft/venv/bin:$PATH PYTHONUNBUFFERED=1 LOFT_CONFIG=/etc/loft/gateway.toml
VOLUME ["/etc/loft", "/var/lib/loft"]
# 8443: direct listener (TLS, pinned key). 8080: public listener, for a tunnel only.
EXPOSE 8443 8080
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3).status==200 else 1)" || exit 1
ENTRYPOINT ["loft-container"]
