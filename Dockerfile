FROM python:3.12-slim

RUN useradd --system --home /data --uid 10001 connector \
 && mkdir -p /data && chown connector /data
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .

USER connector
ENV CONNECTOR_DATA_DIR=/data \
    CONNECTOR_BIND_HOST=0.0.0.0 \
    CONNECTOR_PORT=8765
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/health')"
ENTRYPOINT ["pve-connector"]
CMD ["serve"]
