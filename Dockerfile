FROM python:3.12-slim

# Install ghostscript, cups-filters (for rastertoepson), and network tools
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ghostscript \
    cups-filters \
    curl wget \
    && rm -rf /var/lib/apt/lists/*

# Install pymupdf for PDF page counting/metadata
RUN pip install --no-cache-dir pymupdf

# Copy the Epson 24-pin PPD (for rastertoepson)
COPY epson24.ppd /etc/cups/ppd/epson24.ppd

# Service user
RUN addgroup --gid 1000 epson 2>/dev/null || true && \
    adduser --disabled-password --gecos "" --uid 1000 --gid 1000 epson 2>/dev/null || true

WORKDIR /app
COPY server.py /app/server.py
RUN mkdir -p /share && chown -R epson:epson /app /share

USER epson
EXPOSE 18790

ENV EPSON_MCP_TRANSPORT=stdio
ENV EPSON_MCP_PRINTER_HOST=192.168.4.21
ENV EPSON_MCP_PRINTER_HOST_FALLBACK=192.168.4.21

ENTRYPOINT ["python", "/app/server.py"]
