#!/usr/bin/env bash

if [[ -t 1 ]]; then
  CRAFTARR_BLUE=$'\033[1;34m'
  CRAFTARR_GREEN=$'\033[1;32m'
  CRAFTARR_YELLOW=$'\033[1;33m'
  CRAFTARR_RED=$'\033[1;31m'
  CRAFTARR_BOLD=$'\033[1m'
  CRAFTARR_RESET=$'\033[0m'
else
  CRAFTARR_BLUE=
  CRAFTARR_GREEN=
  CRAFTARR_YELLOW=
  CRAFTARR_RED=
  CRAFTARR_BOLD=
  CRAFTARR_RESET=
fi

banner() {
  printf '\n%s%s============================================================%s\n' "$CRAFTARR_BLUE" "$CRAFTARR_BOLD" "$CRAFTARR_RESET"
  printf ' %s%s%s\n' "$CRAFTARR_BOLD" "$1" "$CRAFTARR_RESET"
  printf '%s%s============================================================%s\n\n' "$CRAFTARR_BLUE" "$CRAFTARR_BOLD" "$CRAFTARR_RESET"
}

section() { printf '\n%s==>%s %s%s%s\n' "$CRAFTARR_BLUE" "$CRAFTARR_RESET" "$CRAFTARR_BOLD" "$*" "$CRAFTARR_RESET"; }
info() { printf '%s[INFO]%s  %s\n' "$CRAFTARR_GREEN" "$CRAFTARR_RESET" "$*"; }
warn() { printf '%s[WARN]%s  %s\n' "$CRAFTARR_YELLOW" "$CRAFTARR_RESET" "$*" >&2; }
error() { printf '%s[ERROR]%s %s\n' "$CRAFTARR_RED" "$CRAFTARR_RESET" "$*" >&2; }
die() { error "$*"; exit 1; }
