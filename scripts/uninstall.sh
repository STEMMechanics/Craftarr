#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd || true)
if [[ -n "$SCRIPT_DIR" && -f "$SCRIPT_DIR/common.sh" ]]; then
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/common.sh"
else
  banner() { printf '\n============================================================\n %s\n============================================================\n\n' "$1"; }
  section() { printf '\n==> %s\n' "$*"; }
  info() { printf '[INFO]  %s\n' "$*"; }
  warn() { printf '[WARN]  %s\n' "$*" >&2; }
  error() { printf '[ERROR] %s\n' "$*" >&2; }
fi

banner "Craftarr Uninstaller"

usage() {
  cat <<EOF
Usage:
  sudo $0 --confirm
  sudo $0 --purge-all --confirm

Options:
  --confirm    Required confirmation for either uninstall mode.
  --purge-all Permanently remove the application, database, configuration,
              upgrade snapshots, backups, and every Minecraft server.

Without --purge-all, application files are removed while all data is preserved.
EOF
}

CONFIRMED=false
PURGE_ALL=false
while (($#)); do
  case "$1" in
    --confirm) CONFIRMED=true ;;
    --purge-all) PURGE_ALL=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this uninstaller as root." >&2
  usage >&2
  exit 1
fi

if [[ "$CONFIRMED" != true && -r /dev/tty ]]; then
  if [[ "$PURGE_ALL" == true ]]; then
    warn "This will permanently delete the database, configuration, backups, and every Minecraft server."
    printf 'Type PURGE to continue: ' >/dev/tty
    read -r answer </dev/tty || true
    [[ "$answer" == "PURGE" ]] && CONFIRMED=true
  else
    printf 'Remove the application while preserving all data? [y/N] ' >/dev/tty
    read -r answer </dev/tty || true
    [[ "$answer" =~ ^[Yy]$ ]] && CONFIRMED=true
  fi
fi
if [[ "$CONFIRMED" != true ]]; then
  error "Uninstall cancelled; confirmation was not provided."
  exit 1
fi

INSTALL_DIR=/opt/craftarr
CONFIG_DIR=/etc/craftarr
DATA_DIR=/var/lib/craftarr
SERVER_DIR=/srv/minecraft
RUNTIME_DIR=/run/craftarr
SERVICE_USER=craftarr
SERVICE_GROUP=craftarr

remove_tree() {
  local target=$1
  if ! rm -rf -- "$target"; then
    echo "Unable to remove $target." >&2
    return 1
  fi
  if [[ -e "$target" ]]; then
    echo "Uninstall failed: $target still exists." >&2
    echo "Check whether it is a read-only mount or has immutable files:" >&2
    echo "  findmnt $target" >&2
    echo "  lsattr -R $target" >&2
    return 1
  fi
}

section "Stopping and disabling services"
systemctl disable --now craftarr-console.service 2>/dev/null || true

if [[ "$PURGE_ALL" == true ]]; then
  info "Stopping all managed Minecraft services"
  systemctl disable --now 'craftarr-server@*.service' 2>/dev/null || true
fi

rm -f /etc/systemd/system/craftarr-console.service /etc/systemd/system/craftarr-server@.service
rm -f /etc/polkit-1/rules.d/50-craftarr-console.rules
rm -f /usr/bin/craftarr-console /usr/local/sbin/craftarr-console
systemctl daemon-reload

section "Removing application files"
info "Removing $INSTALL_DIR"
remove_tree "$INSTALL_DIR"

if [[ "$PURGE_ALL" == true ]]; then
  section "Permanently removing data"
  remove_tree "$DATA_DIR"
  remove_tree "$SERVER_DIR"
  remove_tree "$CONFIG_DIR"
  remove_tree "$RUNTIME_DIR"

  if id "$SERVICE_USER" >/dev/null 2>&1; then
    userdel "$SERVICE_USER"
  fi
  if getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    groupdel "$SERVICE_GROUP" 2>/dev/null || true
  fi

  banner "Craftarr was completely removed"
  cat <<EOF

Deleted permanently:
  $INSTALL_DIR
  $DATA_DIR
  $SERVER_DIR
  $CONFIG_DIR
  $RUNTIME_DIR
  service account: $SERVICE_USER
EOF
  exit 0
fi

banner "Craftarr was removed"
cat <<EOF
Application files were removed successfully.

Preserved data:
  $DATA_DIR
  $SERVER_DIR
  $CONFIG_DIR

Running the installer again will reuse the preserved database, servers, and
administrator credentials. Remove those directories separately only if you
intend to permanently delete their contents.
EOF
