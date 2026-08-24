ARG BASE_IMAGE=python:3.11-alpine3.20
FROM

ARG BUILD_DATE
ARG VCS_REF
ARG VERSION="0.7.0-creator-preview.1"

LABEL org.opencontainers.image.title="Mylar3 Modern Creator Edition"       org.opencontainers.image.description="Automated Comic Book Downloader with Creator Identity Resolution"       org.opencontainers.image.url="https://github.com/burnshroom/mylar3.1"       org.opencontainers.image.source="https://github.com/burnshroom/mylar3.1"       org.opencontainers.image.revision=       org.opencontainers.image.created=       org.opencontainers.image.version=       org.opencontainers.image.licenses="GPL-3.0-only"

RUN apk add --no-cache     bash     curl     git     build-base     libffi-dev     zlib-dev     jpeg-dev     tzdata     shadow     su-exec

WORKDIR /app/mylar3

COPY requirements.txt ./
RUN pip install --no-cache-dir -U pip setuptools wheel &&     pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PUID=1000     PGID=1000     UMASK=002     TZ=Etc/UTC

VOLUME /config /comics /downloads
EXPOSE 8090

ENTRYPOINT ["python3", "/app/mylar3/Mylar.py", "--nolaunch", "--quiet", "--datadir", "/config"]
