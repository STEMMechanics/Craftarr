#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 || $# -ne 1 ]]; then
  echo "Usage: sudo $0 /var/lib/craftarr/upgrades/TIMESTAMP" >&2
  exit 1
fi

INSTALL_DIR=/opt/craftarr
BACKUP_ROOT=/var/lib/craftarr/upgrades
BACKUP_DIR=$(realpath "$1")
case "$BACKUP_DIR" in
  "$BACKUP_ROOT"/*) ;;
  *) echo "Backup must be inside $BACKUP_ROOT." >&2; exit 1 ;;
esac

for item in app migrations alembic.ini requirements.txt; do
  [[ -e "$BACKUP_DIR/$item" ]] || { echo "Incomplete upgrade backup: missing $item" >&2; exit 1; }
done

systemctl stop craftarr-console.service 2>/dev/null || true
cp -a "$BACKUP_DIR/app" "$BACKUP_DIR/migrations" "$BACKUP_DIR/alembic.ini" "$BACKUP_DIR/requirements.txt" "$INSTALL_DIR/"
if [[ -f "$BACKUP_DIR/plugin-monitoring.yml" ]]; then
  cp -a "$BACKUP_DIR/plugin-monitoring.yml" "$INSTALL_DIR/"
fi
if [[ -f "$BACKUP_DIR/craftarr.db" ]]; then
  cp -a "$BACKUP_DIR/craftarr.db" /var/lib/craftarr/craftarr.db
fi
"$INSTALL_DIR/.venv/bin/pip" install --requirement "$INSTALL_DIR/requirements.txt"
chown -R craftarr:craftarr "$INSTALL_DIR" /var/lib/craftarr
systemctl start craftarr-console.service
echo "Rollback complete."
