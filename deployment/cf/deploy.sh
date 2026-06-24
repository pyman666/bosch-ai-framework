#!/bin/bash
# =============================================================================
# Bosch AI Platform — Cloud Foundry 部署脚本
#
# 每个 agent 是独立的 CF App，一次部署一个或全部。
#
# 用法:
#   ./deployment/cf/deploy.sh rag          # 只部署 rag
#   ./deployment/cf/deploy.sh forecast     # 只部署 forecast
#   ./deployment/cf/deploy.sh all          # 部署全部
#
# 前置:
#   cf login -a https://api.cf.eu10.hana.ondemand.com -o <org> -s <space>
#
# 首次部署前需要创建 service instances（一次性）:
#   cf create-service xsuaa application <plan> <instance-name> -c xs-security.json
#   cf create-service redis <plan> <instance-name>
#   # 等等，见各 agent 的 manifest.yml
# =============================================================================

set -euo pipefail

AGENT="${1:-}"
BASE_DIR="$(cd "$(dirname "$0")/../.." && pwd)"

deploy() {
    local name="$1"
    echo ""
    echo "========================================"
    echo "  Deploying: $name"
    echo "========================================"
    cd "$BASE_DIR/$name"
    cf push --strategy rolling
    echo "  $name deployed."
}

if [ -z "$AGENT" ]; then
    echo "Usage: $0 {document|rag|forecast|analytics|all}"
    echo ""
    echo "Agents:"
    echo "  document   — 文档解析（PDF + Excel）"
    echo "  rag        — RAG 知识库检索"
    echo "  forecast   — 预测 / Function Generator"
    echo "  analytics  — AI BI / NL2SQL"
    echo "  all        — 全部"
    exit 1
fi

case "$AGENT" in
    all)
        for a in document rag forecast analytics; do
            deploy "$a"
        done
        ;;
    document|rag|forecast|analytics)
        deploy "$AGENT"
        ;;
    *)
        echo "Unknown agent: $AGENT"
        echo "Valid: document | rag | forecast | analytics | all"
        exit 1
        ;;
esac

echo ""
echo "Done. Check status: cf apps"
