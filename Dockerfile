FROM python:3.13-alpine

RUN apk add make ffmpeg

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /opt/src

COPY pyproject.toml pyproject.toml
COPY nudgisclient nudgisclient
RUN pip install --no-cache-dir --editable '.[dev]'
