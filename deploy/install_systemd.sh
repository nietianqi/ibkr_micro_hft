#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "usage: $0 <project-root> <service-name>"
  exit 1
fi

PROJECT_ROOT="$1"
SERVICE_NAME="$2"
SERVICE_FILE="${PROJECT_ROOT}/deploy/ibkr-micro-alpha.service"

sudo cp "${SERVICE_FILE}" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}.service"
echo "installed ${SERVICE_NAME}.service"
