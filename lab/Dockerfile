FROM python:3.12-slim

ARG SERVICE_BUILD_VERSION=dev
LABEL org.opencontainers.image.version=${SERVICE_BUILD_VERSION}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY services/service.py /app/service.py

RUN useradd --create-home --uid 10001 appuser
USER appuser

EXPOSE 8080
CMD ["python", "/app/service.py"]
