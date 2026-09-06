# Coordinator 的容器映像(設計文件第 9 節)。
#
# 容器化的理由不只是「好部署」:coordinator 部署的那台機器同時是開發機,
# 工作區會被 rebase / checkout 改動。直接從 checkout 跑服務的話,別人一次
# git 操作就換掉了正在服務的程式碼。映像是快照,把這兩件事隔開。
#
# 用 uv.lock 安裝而不是讓 pip 自由解析,這樣重建映像拿到的是同一組相依。

FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先只複製相依定義,讓這層在原始碼變動時仍能命中快取。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# README.md 是 pyproject 宣告的套件檔案之一,少了它 build 會失敗。
COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev

# DB 掛在 volume 上:容器可以被換掉,租約與稽核紀錄不能跟著消失。
ENV COORDINATOR_DB=/data/coordinator.db
VOLUME ["/data"]
EXPOSE 8000

CMD ["uv", "run", "--no-dev", "uvicorn", "coordinator.api:app", \
     "--host", "0.0.0.0", "--port", "8000"]
