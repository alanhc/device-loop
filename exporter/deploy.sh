#!/usr/bin/env bash
# 把這個 checkout 的 exporter 部署到 ~/.local/share/device-loop 並重啟服務。
#
# 為什麼不直接從工作區跑:工作區會被 rebase/checkout 改動,一次 git 操作就
# 換掉了正在服務的程式碼。這裡用 `git archive` 取一份快照,跟 coordinator
# 用容器映像當快照是同一個理由(見 coordinator/Dockerfile 的開頭)。
#
# 用法:
#   ./exporter/deploy.sh            # 部署 HEAD 並重啟
#   ./exporter/deploy.sh --dry-run  # 只看會部署哪個 rev
set -euo pipefail

DEPLOY_DIR="${DEVICE_LOOP_DEPLOY:-$HOME/.local/share/device-loop}"
SERVICE=device-loop-exporter.service
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

cd "$REPO_ROOT"
REV="$(git rev-parse --short HEAD)"

if [[ "${1:-}" == "--dry-run" ]]; then
    echo "would deploy $REV to $DEPLOY_DIR"
    [[ -f "$DEPLOY_DIR/DEPLOYED_REV" ]] && echo "currently deployed: $(cat "$DEPLOY_DIR/DEPLOYED_REV")"
    exit 0
fi

# 未 commit 的改動不會被 git archive 帶上去,講清楚免得以為部署了卻沒有。
if ! git diff-index --quiet HEAD --; then
    echo "warning: uncommitted changes will NOT be deployed (git archive uses HEAD)" >&2
fi

echo "deploying $REV to $DEPLOY_DIR"
mkdir -p "$DEPLOY_DIR"
# coordinator 也要:exporter 的 dev 相依用 path = "../coordinator",
# 少了它 uv 連 --no-dev 都解不動。
git archive HEAD | tar -x -C "$DEPLOY_DIR"
echo "$REV" > "$DEPLOY_DIR/DEPLOYED_REV"

( cd "$DEPLOY_DIR/exporter" && uv sync --frozen --no-dev -q )

if systemctl --user is-enabled --quiet "$SERVICE" 2>/dev/null; then
    systemctl --user restart "$SERVICE"
    echo "restarted $SERVICE"
    systemctl --user --no-pager --lines=0 status "$SERVICE" | head -3
else
    echo "$SERVICE is not enabled; start it with:"
    echo "  systemctl --user enable --now $SERVICE"
fi
