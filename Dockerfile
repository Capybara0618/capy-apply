FROM node:22-alpine AS webui-builder

WORKDIR /build
COPY webui/package.json webui/package-lock.json webui/
RUN cd webui && npm ci
COPY webui/ webui/
RUN cd webui && npm run build


FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md hatch_build.py ./
COPY capybot/ capybot/
COPY --from=webui-builder /build/capybot/web/dist/ capybot/web/dist/

ENV CAPYBOT_SKIP_WEBUI_BUILD=1
RUN uv pip install --system --no-cache .

RUN useradd -m -u 1000 -s /bin/bash capybot && \
    mkdir -p /home/capybot/.capybot && \
    chown -R capybot:capybot /home/capybot /app

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/entrypoint.sh && chmod +x /usr/local/bin/entrypoint.sh

USER capybot
ENV HOME=/home/capybot

EXPOSE 8765

ENTRYPOINT ["entrypoint.sh"]
CMD ["apply", "serve", "--no-open"]
