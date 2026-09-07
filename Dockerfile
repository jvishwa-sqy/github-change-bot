FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install --no-install-recommends -y git gosu openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 gitbot \
    && useradd --uid 10001 --gid gitbot --create-home --shell /usr/sbin/nologin gitbot

WORKDIR /app
COPY pyproject.toml ./
COPY app ./app
COPY scripts/bootstrap_repo.py ./scripts/bootstrap_repo.py
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint
RUN python -m pip install . \
    && mkdir -p /var/lib/git-change-bot \
    && chown -R gitbot:gitbot /var/lib/git-change-bot \
    && chmod 0755 /usr/local/bin/docker-entrypoint

VOLUME ["/var/lib/git-change-bot"]
EXPOSE 8088

ENTRYPOINT ["/usr/local/bin/docker-entrypoint"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8088", "--proxy-headers", "--forwarded-allow-ips", "*"]
