#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: sudo ./scripts/install.sh [OPTIONS]

Installs Craftarr on Ubuntu or Oracle Linux. By default the installer
uses apt or dnf to install Python 3.10+, polkit and supporting tools. Java is
installed only when selected interactively or with --java-version.

Options:
  --host ADDRESS     Bind address (default: prompted, then 0.0.0.0).
  --port PORT        Web port from 1024 to 65535 (default: prompted, then 8000).
  --non-interactive  Accept defaults instead of prompting.
  --java-version N   Install Java N if missing; repeat for multiple versions.
                     Supported versions: 8, 11, 17, 21, 25.
  --skip-packages    Do not install operating-system packages.
  -h, --help         Show this help.
EOF
}

SKIP_PACKAGES=false
NON_INTERACTIVE=false
JAVA_VERSIONS=()
BIND_HOST=
WEB_PORT=
DEFAULT_BIND_HOST=0.0.0.0
INSTALL_ARGS=()
while (($#)); do
  case "$1" in
    --skip-packages)
      SKIP_PACKAGES=true
      INSTALL_ARGS+=(--skip-packages)
      ;;
    --non-interactive)
      NON_INTERACTIVE=true
      INSTALL_ARGS+=(--non-interactive)
      ;;
    --java-version)
      [[ $# -ge 2 ]] || { echo "--java-version requires a version." >&2; exit 2; }
      [[ "$2" =~ ^(8|11|17|21|25)$ ]] || {
        echo "Unsupported Java version '$2'. Choose 8, 11, 17, 21, or 25." >&2
        exit 2
      }
      JAVA_VERSIONS+=("$2")
      INSTALL_ARGS+=(--java-version "$2")
      shift
      ;;
    --host)
      [[ $# -ge 2 ]] || { echo "--host requires an address." >&2; exit 2; }
      BIND_HOST=$2
      INSTALL_ARGS+=(--host "$2")
      shift
      ;;
    --port)
      [[ $# -ge 2 ]] || { echo "--port requires a value." >&2; exit 2; }
      WEB_PORT=$2
      INSTALL_ARGS+=(--port "$2")
      shift
      ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root." >&2
  exit 1
fi

SOURCE_DIR=
if [[ -n "${BASH_SOURCE[0]:-}" ]]; then
  SOURCE_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
fi

# When install.sh is piped directly to Bash it has no accompanying checkout.
# Fetch a temporary copy and hand the original arguments to that copy. When
# run from a checkout, continue directly with the local source instead.
if [[ -z "$SOURCE_DIR" || ! -d "$SOURCE_DIR/app" || ! -f "$SOURCE_DIR/requirements.txt" ]]; then
  REPOSITORY=${CRAFTARR_CONSOLE_REPOSITORY:-STEMMechanics/Craftarr}

  for command in curl tar mktemp find; do
    command -v "$command" >/dev/null 2>&1 || {
      echo "Required bootstrap command not found: $command" >&2
      exit 1
    }
  done

  if [[ -n "${CRAFTARR_CONSOLE_REF:-}" ]]; then
    REF=$CRAFTARR_CONSOLE_REF
    REF_TYPE=heads
    DOWNLOAD_LABEL="development ref"
  else
    if ! LATEST_RELEASE_URL=$(curl --fail --silent --show-error --location \
      --proto '=https' --tlsv1.2 --output /dev/null --write-out '%{url_effective}' \
      "https://github.com/$REPOSITORY/releases/latest"); then
      echo "Unable to resolve the latest published Craftarr release." >&2
      exit 1
    fi
    REF=${LATEST_RELEASE_URL##*/}
    REF_TYPE=tags
    DOWNLOAD_LABEL="release"
  fi

  if [[ ! "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$ || "$REF" == *".."* || "$REF" == *"//"* ]]; then
    echo "GitHub returned an invalid release or source reference: $REF" >&2
    exit 1
  fi

  TEMP_DIR=$(mktemp -d /tmp/craftarr-console-install.XXXXXX)
  cleanup() {
    rm -rf -- "$TEMP_DIR"
  }
  trap cleanup EXIT

  ARCHIVE="$TEMP_DIR/source.tar.gz"
  SOURCE_URL="https://github.com/$REPOSITORY/archive/refs/$REF_TYPE/$REF.tar.gz"
  echo "Downloading Craftarr $DOWNLOAD_LABEL $REF from $REPOSITORY..."
  curl --fail --silent --show-error --location --proto '=https' --tlsv1.2 \
    "$SOURCE_URL" --output "$ARCHIVE"
  tar -xzf "$ARCHIVE" -C "$TEMP_DIR"

  REMOTE_INSTALLER=$(find "$TEMP_DIR" -mindepth 2 -maxdepth 3 -type f -path '*/scripts/install.sh' -print -quit)
  if [[ -z "$REMOTE_INSTALLER" ]]; then
    echo "The downloaded source does not contain scripts/install.sh." >&2
    exit 1
  fi

  chmod +x "$REMOTE_INSTALLER"
  "$REMOTE_INSTALLER" "${INSTALL_ARGS[@]}"
  exit $?
fi

# shellcheck disable=SC1091
source "$SOURCE_DIR/scripts/common.sh"
banner "Craftarr Installer"

if [[ ! -r /etc/os-release ]]; then
  echo "Unable to identify this Linux distribution." >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
OS_ID=${ID,,}
OS_VERSION=${VERSION_ID%%.*}

case "$OS_ID" in
  ubuntu)
    if ((OS_VERSION < 22)); then
      echo "Ubuntu 22.04 or newer is required." >&2
      exit 1
    fi
    PACKAGE_MANAGER=apt
    PYTHON_BIN=python3
    PACKAGES=(python3 python3-venv python3-pip policykit-1 ca-certificates curl gnupg)
    ;;
  ol|oracle|oraclelinux)
    if ((OS_VERSION < 8)); then
      echo "Oracle Linux 8 or newer is required." >&2
      exit 1
    fi
    PACKAGE_MANAGER=dnf
    PYTHON_BIN=python3.11
    PACKAGES=(python3.11 python3.11-pip polkit ca-certificates curl)
    ;;
  *)
    echo "Unsupported distribution '$OS_ID'. Supported: Ubuntu and Oracle Linux." >&2
    exit 1
    ;;
esac

if [[ "$SKIP_PACKAGES" == false ]]; then
  section "Installing operating-system packages"
  if [[ "$PACKAGE_MANAGER" == apt ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends "${PACKAGES[@]}"
  else
    dnf install -y "${PACKAGES[@]}"
  fi
fi

java_major_installed() {
  local wanted=$1 candidate output version
  while IFS= read -r candidate; do
    [[ -x "$candidate" ]] || continue
    output=$("$candidate" -version 2>&1 | head -1 || true)
    version=$(sed -n 's/.*version "\([0-9][0-9]*\).*/\1/p' <<<"$output")
    [[ "$version" == 1 ]] && version=$(sed -n 's/.*version "1\.\([0-9][0-9]*\).*/\1/p' <<<"$output")
    [[ "$version" == "$wanted" ]] && return 0
  done < <(find /usr/lib/jvm /usr/java /opt/java -type f -path '*/bin/java' 2>/dev/null; command -v java 2>/dev/null || true)
  return 1
}

if [[ "$SKIP_PACKAGES" == false && ${#JAVA_VERSIONS[@]} -eq 0 && "$NON_INTERACTIVE" == false && -r /dev/tty ]]; then
  answer=
  printf 'Java versions to install if missing (8,11,17,21,25; blank for none): ' >/dev/tty
  read -r answer </dev/tty || true
  answer=${answer//,/ }
  for version in $answer; do
    [[ "$version" =~ ^(8|11|17|21|25)$ ]] || die "Unsupported Java version '$version'."
    JAVA_VERSIONS+=("$version")
  done
fi

JAVA_TO_INSTALL=()
if [[ "$SKIP_PACKAGES" == false ]]; then
  for version in "${JAVA_VERSIONS[@]}"; do
    if java_major_installed "$version"; then
      info "Java $version is already installed; preserving it."
    elif [[ ! " ${JAVA_TO_INSTALL[*]} " == *" $version "* ]]; then
      JAVA_TO_INSTALL+=("$version")
    fi
  done
fi

if (( ${#JAVA_TO_INSTALL[@]} )); then
    section "Installing selected Java runtimes: ${JAVA_TO_INSTALL[*]}"
    if [[ "$PACKAGE_MANAGER" == apt ]]; then
      curl --fail --silent --show-error https://apt.corretto.aws/corretto.key |
        gpg --dearmor --yes --output /usr/share/keyrings/corretto-keyring.gpg
      echo 'deb [signed-by=/usr/share/keyrings/corretto-keyring.gpg] https://apt.corretto.aws stable main' \
        > /etc/apt/sources.list.d/corretto.list
      apt-get update
      JAVA_PACKAGES=()
      for version in "${JAVA_TO_INSTALL[@]}"; do
        JAVA_PACKAGES+=("java-$version-amazon-corretto-jdk")
      done
      apt-get install -y --no-install-recommends "${JAVA_PACKAGES[@]}" libxi6 libxtst6 libxrender1
    else
      rpm --import https://yum.corretto.aws/corretto.key
      curl --fail --silent --show-error --location \
        https://yum.corretto.aws/corretto.repo --output /etc/yum.repos.d/corretto.repo
      JAVA_PACKAGES=()
      for version in "${JAVA_TO_INSTALL[@]}"; do
        JAVA_PACKAGES+=("java-$version-amazon-corretto-devel")
      done
      dnf install -y "${JAVA_PACKAGES[@]}"
    fi
fi

for command in "$PYTHON_BIN" systemctl useradd install cp runuser; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "Required command not found: $command" >&2
    exit 1
  }
done

PYTHON_VERSION=$(
  "$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
)
"$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || {
  echo "Python 3.10 or newer is required; found $PYTHON_VERSION." >&2
  exit 1
}

INSTALL_DIR=/opt/craftarr
DATA_DIR=/var/lib/craftarr
SERVER_DIR=/srv/minecraft
CONFIG_DIR=/etc/craftarr
SERVICE_USER=craftarr
SERVICE_GROUP=craftarr

for required in app migrations alembic.ini requirements.txt plugin-monitoring.yml deploy/craftarr-console.service deploy/craftarr-server@.service deploy/50-craftarr-console.rules deploy/craftarr-console scripts/common.sh; do
  [[ -e "$SOURCE_DIR/$required" ]] || {
    echo "Installation source is incomplete: missing $required" >&2
    exit 1
  }
done

MIGRATION_FILE=$(find "$SOURCE_DIR/migrations/versions" -maxdepth 1 -type f -name '*.py' -print -quit 2>/dev/null || true)
if [[ -z "$MIGRATION_FILE" ]]; then
  echo "Installation source is incomplete: no database migrations were found." >&2
  exit 1
fi

REPAIR_INSTALL=false
if [[ -e "$INSTALL_DIR/app" ]]; then
  REPAIR_INSTALL=true
  section "Existing installation found; repairing application and service files"
  systemctl stop craftarr-console.service 2>/dev/null || true
fi

CONFIG_FILE=$CONFIG_DIR/console.env
if [[ -f "$CONFIG_FILE" ]]; then
  CONFIGURED_HOST=$(sed -n 's/^CRAFTARR_CONSOLE_HOST=//p' "$CONFIG_FILE" | tail -1)
  CONFIGURED_PORT=$(sed -n 's/^CRAFTARR_CONSOLE_PORT=//p' "$CONFIG_FILE" | tail -1)
else
  CONFIGURED_HOST=
  CONFIGURED_PORT=
fi

BIND_HOST=${BIND_HOST:-${CONFIGURED_HOST:-$DEFAULT_BIND_HOST}}
WEB_PORT=${WEB_PORT:-${CONFIGURED_PORT:-8000}}

if [[ "$REPAIR_INSTALL" == false && "$NON_INTERACTIVE" == false && -r /dev/tty ]]; then
  answer=
  printf 'Bind address [%s]: ' "$BIND_HOST" >/dev/tty
  read -r answer </dev/tty || true
  BIND_HOST=${answer:-$BIND_HOST}
  answer=
  printf 'Web port [%s]: ' "$WEB_PORT" >/dev/tty
  read -r answer </dev/tty || true
  WEB_PORT=${answer:-$WEB_PORT}
fi

"$PYTHON_BIN" - "$BIND_HOST" <<'PY' || die "Bind address must be a valid IPv4 or IPv6 address."
import ipaddress
import sys
ipaddress.ip_address(sys.argv[1])
PY
[[ "$WEB_PORT" =~ ^[0-9]+$ ]] && ((WEB_PORT >= 1024 && WEB_PORT <= 65535)) || \
  die "Web port must be a number from 1024 to 65535."

info "Web service will bind to $BIND_HOST:$WEB_PORT"

NOLOGIN_SHELL=$(command -v nologin || true)
NOLOGIN_SHELL=${NOLOGIN_SHELL:-/sbin/nologin}
if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  if getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA_DIR" --shell "$NOLOGIN_SHELL" --gid "$SERVICE_GROUP" "$SERVICE_USER"
  else
    useradd --system --home-dir "$DATA_DIR" --shell "$NOLOGIN_SHELL" --user-group "$SERVICE_USER"
  fi
fi
if getent group systemd-journal >/dev/null 2>&1; then
  usermod -a -G systemd-journal "$SERVICE_USER"
fi

install -d -m 0755 -o root -g root "$INSTALL_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$DATA_DIR" "$DATA_DIR/upgrades" "$SERVER_DIR"
install -d -m 0750 -o root -g "$SERVICE_GROUP" "$CONFIG_DIR"
section "Installing Craftarr application"
cp -a "$SOURCE_DIR/app" "$SOURCE_DIR/migrations" "$SOURCE_DIR/alembic.ini" "$SOURCE_DIR/requirements.txt" "$INSTALL_DIR/"
if [[ ! -f "$INSTALL_DIR/plugin-monitoring.yml" ]]; then
  cp -a "$SOURCE_DIR/plugin-monitoring.yml" "$INSTALL_DIR/"
fi

if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip
"$INSTALL_DIR/.venv/bin/python" -m pip install --requirement "$INSTALL_DIR/requirements.txt"

LEGACY_ADMIN_USER=
LEGACY_ADMIN_PASSWORD=
if [[ ! -f "$CONFIG_FILE" ]]; then
  SECRET=$("$INSTALL_DIR/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(48))')
  install -m 0640 -o root -g "$SERVICE_GROUP" /dev/null "$CONFIG_FILE"
  {
    echo "CRAFTARR_CONSOLE_SECRET=$SECRET"
    echo "CRAFTARR_CONSOLE_DATABASE=$DATA_DIR/craftarr.db"
    echo "CRAFTARR_CONSOLE_SERVER_ROOT=$SERVER_DIR"
    echo "CRAFTARR_CONSOLE_HOST=$BIND_HOST"
    echo "CRAFTARR_CONSOLE_PORT=$WEB_PORT"
    # Enable secure cookies after an HTTPS reverse proxy has been configured.
    echo "CRAFTARR_CONSOLE_COOKIE_SECURE=false"
  } > "$CONFIG_FILE"
else
  LEGACY_ADMIN_USER=$(sed -n 's/^CRAFTARR_CONSOLE_ADMIN_USER=//p' "$CONFIG_FILE" | tail -1)
  LEGACY_ADMIN_PASSWORD=$(sed -n 's/^CRAFTARR_CONSOLE_ADMIN_PASSWORD=//p' "$CONFIG_FILE" | tail -1)
  sed -i '/^CRAFTARR_CONSOLE_HOST=/d;/^CRAFTARR_CONSOLE_PORT=/d' "$CONFIG_FILE"
  {
    echo "CRAFTARR_CONSOLE_HOST=$BIND_HOST"
    echo "CRAFTARR_CONSOLE_PORT=$WEB_PORT"
  } >> "$CONFIG_FILE"
fi

section "Preparing database and administrator account"
(
  cd "$INSTALL_DIR"
  runuser -u "$SERVICE_USER" -- env \
    CRAFTARR_CONSOLE_ENV="$CONFIG_FILE" \
    "$INSTALL_DIR/.venv/bin/python" -c \
    'from app.migrations import upgrade_database; upgrade_database()'
)

INITIAL_ADMIN_USER=${LEGACY_ADMIN_USER:-admin}
INITIAL_ADMIN_PASSWORD=$(
  cd "$INSTALL_DIR"
  runuser -u "$SERVICE_USER" -- env \
    CRAFTARR_CONSOLE_ENV="$CONFIG_FILE" \
    CRAFTARR_BOOTSTRAP_ADMIN_PASSWORD="$LEGACY_ADMIN_PASSWORD" \
    "$INSTALL_DIR/.venv/bin/python" -m app.admin_cli ensure-admin \
    --username "$INITIAL_ADMIN_USER"
)
sed -i '/^CRAFTARR_CONSOLE_ADMIN_USER=/d;/^CRAFTARR_CONSOLE_ADMIN_PASSWORD=/d' "$CONFIG_FILE"

section "Installing systemd services"
install -m 0644 "$SOURCE_DIR/deploy/craftarr-console.service" /etc/systemd/system/craftarr-console.service
install -m 0644 "$SOURCE_DIR/deploy/craftarr-server@.service" /etc/systemd/system/craftarr-server@.service
install -d -m 0755 /etc/polkit-1/rules.d
install -m 0644 "$SOURCE_DIR/deploy/50-craftarr-console.rules" /etc/polkit-1/rules.d/50-craftarr-console.rules
install -m 0755 "$SOURCE_DIR/deploy/craftarr-console" /usr/bin/craftarr-console
install -m 0755 "$SOURCE_DIR/deploy/craftarr-console" /usr/local/sbin/craftarr-console

chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR"
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$DATA_DIR" "$SERVER_DIR"
systemctl daemon-reload
systemctl enable --now craftarr-console.service

READY=false
HEALTH_HOST=$BIND_HOST
[[ "$HEALTH_HOST" == "0.0.0.0" ]] && HEALTH_HOST=127.0.0.1
[[ "$HEALTH_HOST" == "::" ]] && HEALTH_HOST=::1
if [[ "$HEALTH_HOST" == *:* ]]; then
  HEALTH_URL="http://[$HEALTH_HOST]:$WEB_PORT/health"
else
  HEALTH_URL="http://$HEALTH_HOST:$WEB_PORT/health"
fi
for _attempt in {1..30}; do
  if curl --fail --silent --globoff --noproxy '*' "$HEALTH_URL" >/dev/null; then
    READY=true
    break
  fi
  if ! systemctl is-active --quiet craftarr-console.service; then
    break
  fi
  sleep 1
done

if [[ "$READY" != true ]]; then
  echo >&2
  echo "Craftarr was installed, but its service did not start." >&2
  echo "No application data or login details have been removed." >&2
  if [[ -n "$INITIAL_ADMIN_PASSWORD" ]]; then
    printf 'Initial administrator: %s\nInitial password: %s\n' "$INITIAL_ADMIN_USER" "$INITIAL_ADMIN_PASSWORD" >&2
  fi
  echo >&2
  echo "Useful recovery commands:" >&2
  echo "  sudo journalctl -u craftarr-console.service --no-pager -n 200" >&2
  echo "  sudo systemctl restart craftarr-console.service" >&2
  echo "  curl -fsSL https://raw.githubusercontent.com/STEMMechanics/Craftarr/main/scripts/install.sh | sudo bash" >&2
  echo >&2
  echo "Recent service logs:" >&2
  journalctl -u craftarr-console.service --no-pager -n 100 >&2
  exit 1
fi

banner "Craftarr installation complete"
cat <<EOF
The service is bound to $BIND_HOST:$WEB_PORT.

Next steps:
  1. Configure an HTTPS reverse proxy to $BIND_HOST:$WEB_PORT.
  2. Open that HTTPS address and sign in with the details below.
  3. Set CRAFTARR_CONSOLE_COOKIE_SECURE=true in
     /etc/craftarr/console.env and restart the service.

Service commands:
  sudo craftarr-console status
  sudo craftarr-console restart
  sudo craftarr-console logs
  sudo craftarr-console reset-password [USERNAME]
EOF

if [[ -n "$INITIAL_ADMIN_PASSWORD" ]]; then
  printf '\nInitial administrator: %s\nInitial password: %s\n' "$INITIAL_ADMIN_USER" "$INITIAL_ADMIN_PASSWORD"
  echo "This password is shown once and stored only as a hash in the database."
  echo "You must change it when you first sign in."
elif [[ "$REPAIR_INSTALL" == true ]]; then
  echo
  echo "This was a repair installation; use your existing administrator login."
fi
