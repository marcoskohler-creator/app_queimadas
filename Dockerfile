# Imagem base já com GDAL instalado (evita dor de cabeça de compilar GDAL)
FROM ghcr.io/osgeo/gdal:ubuntu-small-3.9.2

WORKDIR /srv

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3-pip python3-dev build-essential && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY app ./app
COPY static ./static

ENV JOBS_DIR=/data/jobs
RUN mkdir -p /data/jobs

EXPOSE 8000

# Render/Railway definem a variável $PORT automaticamente; usamos 8000 como padrão local
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
