#!/bin/bash
# ============================================================================
#  📊 FlowPilot Live Monitor
# ============================================================================

# kubectl --kubeconfig .kube/flowpilot-config logs -n flow-pilot deploy/flow-pilot-backend --tail=50 2>&1

# KUBECONFIG=.kube/flowpilot-config kubectl logs -n flow-pilot -l 'app in (flow-pilot-backend,flow-pilot-frontend)' --tail=200 2>&1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
export KUBECONFIG="${KUBECONFIG:-$PROJECT_ROOT/.kube/flowpilot-config}"
NAMESPACE="${NAMESPACE:-flow-pilot}"
LOG_SELECTOR="${LOG_SELECTOR:-app in (flow-pilot-backend,flow-pilot-frontend)}"
SINCE_WINDOW="${SINCE_WINDOW:-1h}"
MAX_LOG_REQUESTS="${MAX_LOG_REQUESTS:-10}"

if ! command -v kubectl >/dev/null 2>&1; then
    echo "kubectl is not installed or not in PATH"
    exit 1
fi

if ! kubectl get pods -n "$NAMESPACE" >/dev/null 2>&1; then
    echo "Cannot access namespace '$NAMESPACE' with kubeconfig '$KUBECONFIG'"
    exit 1
fi

if ! kubectl get pods -n "$NAMESPACE" -l "$LOG_SELECTOR" --no-headers 2>/dev/null | grep -q .; then
    echo "No pods match selector '$LOG_SELECTOR' in namespace '$NAMESPACE'"
    echo "Set LOG_SELECTOR to override, for example: LOG_SELECTOR='app=flow-pilot-backend'"
    exit 1
fi

echo ""
echo -e "\033[36m╔════════════════════════════════════════════════════════════════╗\033[0m"
echo -e "\033[36m║\033[0m  \033[1;37m📊 FLOWPILOT LIVE MONITOR\033[0m                                    \033[36m║\033[0m"
echo -e "\033[36m║\033[0m  \033[90mPress Ctrl+C to exit\033[0m                                         \033[36m║\033[0m"
echo -e "\033[36m╚════════════════════════════════════════════════════════════════╝\033[0m"
echo -e "\033[90m   Namespace: $NAMESPACE\033[0m"
echo -e "\033[90m   Selector:  $LOG_SELECTOR\033[0m"
echo ""

# Show pods
echo -e "\033[1;37m📈 Pods:\033[0m"
kubectl get pods -n "$NAMESPACE" --no-headers 2>/dev/null | while read line; do
    name=$(echo $line | awk '{print $1}')
    status=$(echo $line | awk '{print $3}')
    if [[ "$status" == "Running" ]]; then
        echo -e "   \033[32m●\033[0m $name"
    else
        echo -e "   \033[33m●\033[0m $name ($status)"
    fi
done
echo ""
echo -e "\033[36m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m"
echo ""

# Stream ALL logs from ALL worker pods with simple color highlights
echo -e "\033[90mInitializing log stream (showing last $SINCE_WINDOW + live updates)...\033[0m"
kubectl logs -n "$NAMESPACE" -l "$LOG_SELECTOR" -f --max-log-requests="$MAX_LOG_REQUESTS" --ignore-errors --prefix --since="$SINCE_WINDOW" 2>&1 | \
    sed -u \
        -e 's/\(.*ERROR.*\)/\x1b[31m\1\x1b[0m/' \
        -e 's/\(.*Error.*\)/\x1b[31m\1\x1b[0m/' \
        -e 's/\(.*WARNING.*\)/\x1b[33m\1\x1b[0m/' \
        -e 's/\(.*succeeded.*\)/\x1b[32m\1\x1b[0m/' \
        -e 's/\(.*success.*\)/\x1b[32m\1\x1b[0m/' \
        -e 's/\(.*✅.*\)/\x1b[32m\1\x1b[0m/' \
        -e 's/\(.*�.*\)/\x1b[35m\1\x1b[0m/' \
        -e 's/\(.*📄.*\)/\x1b[36m\1\x1b[0m/' \
        -e 's/\(.*🧠.*\)/\x1b[34m\1\x1b[0m/' \
        -e 's/\(.*📤.*\)/\x1b[34m\1\x1b[0m/' \
        -e 's/\(.*user_phone.*\)/\x1b[35m\1\x1b[0m/' \
        -e 's/\(.*whatsapp_send.*\)/\x1b[32m\1\x1b[0m/' \
        -e 's/\(.*chunk.*\)/\x1b[34m\1\x1b[0m/' \
        -e 's/\(.*embedding.*\)/\x1b[34m\1\x1b[0m/' \
        -e 's/\(.*pgvector.*\)/\x1b[34m\1\x1b[0m/'
