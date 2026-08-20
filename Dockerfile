FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml VERSION README.md LICENSE NOTICE ./
COPY plugins/cw/VERSION plugins/cw/VERSION
COPY cw ./cw

RUN python -m pip install --no-cache-dir ".[remote]" \
    && groupadd --gid 1000 cw \
    && useradd --uid 1000 --gid cw --create-home --shell /bin/bash cw \
    && mkdir -p /var/lib/cw \
    && chown -R cw:cw /var/lib/cw /app

USER cw

EXPOSE 10000

CMD ["python", "-m", "cw.remote.deployment"]
