#!/usr/bin/env bash
# Установка ежедневной синхронизации каталога WB через systemd (переживает перезагрузки).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
test -x "${ROOT}/.venv/bin/python3" || {
  echo "Нет ${ROOT}/.venv/bin/python3 — создайте venv: python3 -m venv .venv && pip install -r requirements.txt" >&2
  exit 1
}
sudo cp "${ROOT}/deploy/wb-catalog-sync.service" /etc/systemd/system/
sudo cp "${ROOT}/deploy/wb-catalog-sync.timer" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable wb-catalog-sync.timer
sudo systemctl start wb-catalog-sync.timer
echo "Таймер включён. Статус:"
systemctl status wb-catalog-sync.timer --no-pager || true
echo "Ближайшие запуски:"
systemctl list-timers wb-catalog-sync.timer --no-pager || true
