#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Pulling latest code..."
git -C "$REPO_DIR" pull

echo "==> Rebuilding image..."
docker compose -f "$REPO_DIR/docker-compose.yml" build

echo "==> Restarting container..."
docker compose -f "$REPO_DIR/docker-compose.yml" up -d

echo "==> Done. Health check..."
sleep 2
curl -sf http://127.0.0.1:5005/health && echo " ✓ healthy" || echo " ✗ health check failed"
