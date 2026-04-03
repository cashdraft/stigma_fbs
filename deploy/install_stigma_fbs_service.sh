#!/usr/bin/env bash
# Установка автозапуска веб-приложения Stigma FBS (systemd).
# Запуск: sudo bash /srv/stigma_fbs/deploy/install_stigma_fbs_service.sh

set -euo pipefail
ROOT="/srv/stigma_fbs"
mkdir -p "${ROOT}/logs"
cp "${ROOT}/deploy/stigma-fbs.service" /etc/systemd/system/stigma-fbs.service
systemctl daemon-reload
systemctl enable stigma-fbs.service
systemctl restart stigma-fbs.service
systemctl --no-pager status stigma-fbs.service
