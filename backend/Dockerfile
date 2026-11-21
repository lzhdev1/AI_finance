FROM python:3.12-slim

# 国内网络可在 build 时覆盖：--build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

COPY pyproject.toml ./
RUN mkdir -p src/quantlab && touch src/quantlab/__init__.py \
    && python -m pip install --index-url "$PIP_INDEX_URL" -e ".[dev]"

COPY src ./src
COPY configs ./configs
COPY scripts ./scripts
COPY tests ./tests
COPY db ./db

RUN mkdir -p data logs

EXPOSE 8000

# 默认起 API；调度器服务在 compose 里覆盖 command
CMD ["uvicorn", "quantlab.api:app", "--host", "0.0.0.0", "--port", "8000"]
