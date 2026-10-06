#!/usr/bin/env bash
# PauruBot installer and manager.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/NexusGuide/PauruBot/main/install.sh)
#
# Without arguments it opens a menu. Every action also works non-interactively:
#   install.sh install --domain bot.example.com --token 123:ABC --owner 123456 [--channel stable|beta] [--version v1.2.3]
#   install.sh update [--channel stable|beta] [--version v1.2.3]
#   install.sh rollback | status | logs [service] | restart | backup | restore FILE | setup-link | uninstall

set -Eeuo pipefail

REPO="NexusGuide/PauruBot"
IMAGE_REPO="ghcr.io/nexusguide/paurubot"
DIR="${PAURUBOT_DIR:-/opt/paurubot}"
ENV_FILE="$DIR/.env"

if [[ -t 1 ]]; then
  BOLD=$'\e[1m' DIM=$'\e[2m' RED=$'\e[31m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RESET=$'\e[0m'
else
  BOLD="" DIM="" RED="" GREEN="" YELLOW="" RESET=""
fi

say() { printf '%s\n' "$*"; }
ok() { printf '%s✔%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '%s!%s %s\n' "$YELLOW" "$RESET" "$*" >&2; }
die() {
  printf '%s✘ %s%s\n' "$RED" "$*" "$RESET" >&2
  exit 1
}
trap 'die "stopped at line $LINENO: $BASH_COMMAND"' ERR

need_root() { [[ $EUID -eq 0 ]] || die "run as root (sudo -i)"; }

compose() { docker compose --project-directory "$DIR" --env-file "$ENV_FILE" "$@"; }

env_get() { { grep -E "^$1=" "$ENV_FILE" 2>/dev/null || true; } | tail -n1 | cut -d= -f2-; }

env_set() {
  local key=$1 value=$2 tmp
  tmp=$(mktemp "$DIR/.env.XXXXXX")
  if grep -qE "^$key=" "$ENV_FILE" 2>/dev/null; then
    awk -v k="$key" -v v="$value" 'BEGIN { FS = OFS = "=" } $1 == k { print k "=" v; next } { print }' "$ENV_FILE" >"$tmp"
  else
    cat "$ENV_FILE" >"$tmp" 2>/dev/null || true
    printf '%s=%s\n' "$key" "$value" >>"$tmp"
  fi
  chmod 600 "$tmp"
  mv "$tmp" "$ENV_FILE"
}

random_secret() { openssl rand -base64 "${1:-48}" | tr -d '\n=/+' | cut -c1-"${2:-64}"; }

ask() {
  local prompt=$1 default=${2:-} reply
  if [[ -n $default ]]; then
    read -r -p "$prompt [$default]: " reply </dev/tty || true
    printf '%s' "${reply:-$default}"
  else
    while [[ -z ${reply:-} ]]; do read -r -p "$prompt: " reply </dev/tty || true; done
    printf '%s' "$reply"
  fi
}

confirm() {
  local reply
  read -r -p "$1 [y/N]: " reply </dev/tty || true
  [[ $reply =~ ^[Yy]([Ee][Ss])?$ ]]
}

# ---------------------------------------------------------------- prerequisites

install_docker() {
  if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
    return
  fi
  say "${BOLD}Installing Docker…${RESET}"
  if curl -fsSL https://get.docker.com | sh; then
    systemctl enable --now docker >/dev/null 2>&1 || true
  else
    warn "get.docker.com is unreachable; trying the distribution packages"
    apt-get update -q && apt-get install -yq docker.io docker-compose-v2 || apt-get install -yq docker.io docker-compose-plugin
  fi
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
  ok "Docker is ready"
}

check_system() {
  command -v curl >/dev/null || { apt-get update -q && apt-get install -yq curl; }
  command -v openssl >/dev/null || apt-get install -yq openssl
  command -v git >/dev/null || apt-get install -yq git
  local mem
  mem=$(awk '/MemTotal/ { print int($2 / 1024) }' /proc/meminfo)
  ((mem >= 900)) || warn "only ${mem} MB RAM; 1 GB or more is recommended"
}

# ---------------------------------------------------------------- releases

latest_release() {
  curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1
}

# Resolve the git ref and the image for a channel or an explicit version.
resolve_target() {
  local channel=$1 version=$2
  if [[ -n $version ]]; then
    TARGET_REF=$version
    TARGET_IMAGE="$IMAGE_REPO:${version#v}"
  elif [[ $channel == beta ]]; then
    TARGET_REF=main
    TARGET_IMAGE="$IMAGE_REPO:edge"
  else
    TARGET_REF=$(latest_release || true)
    if [[ -z $TARGET_REF ]]; then
      warn "no stable release published yet; using the beta channel"
      TARGET_REF=main
      TARGET_IMAGE="$IMAGE_REPO:edge"
    else
      TARGET_IMAGE="$IMAGE_REPO:${TARGET_REF#v}"
    fi
  fi
}

fetch_source() {
  local ref=$1
  if [[ -d $DIR/.git ]]; then
    git -C "$DIR" fetch --depth 1 origin "$ref" -q
    git -C "$DIR" checkout -q --force FETCH_HEAD
  else
    mkdir -p "$DIR"
    git clone -q --depth 1 --branch "$ref" "https://github.com/$REPO.git" "$DIR.tmp"
    cp -a "$DIR.tmp/." "$DIR/"
    rm -rf "$DIR.tmp"
  fi
}

# Pull the release image and pin it by digest; build locally when the registry is unreachable.
fetch_image() {
  local image=$1 digest
  if docker pull -q "$image" >/dev/null 2>&1; then
    digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$image" 2>/dev/null || true)
    if command -v cosign >/dev/null && [[ -n $digest ]]; then
      cosign verify "$digest" \
        --certificate-identity-regexp "^https://github.com/$REPO/.github/workflows/release.yml@" \
        --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null ||
        die "the image signature does not verify; refusing to run it"
      ok "image signature verified"
    fi
    PINNED_IMAGE=${digest:-$image}
  else
    warn "cannot pull $image; building the image on this server"
    compose build -q api
    PINNED_IMAGE="paurubot:local"
  fi
}

# Docker Hub refuses many Iranian addresses; fall back to the copies published next to our image.
pick_base_images() {
  local mirror="ghcr.io/nexusguide/paurubot-mirror" pair key image
  for pair in POSTGRES_IMAGE=postgres:16-alpine REDIS_IMAGE=redis:7-alpine CADDY_IMAGE=caddy:2-alpine; do
    key=${pair%%=*}
    image=${pair#*=}
    if docker pull -q "$image" >/dev/null 2>&1; then
      env_set "$key" "$image"
    elif docker pull -q "$mirror:${image/:/-}" >/dev/null 2>&1; then
      env_set "$key" "$mirror:${image/:/-}"
      ok "using the GHCR copy of $image"
    else
      die "cannot download $image from Docker Hub or GHCR; set a registry mirror in /etc/docker/daemon.json"
    fi
  done
}

wait_healthy() {
  local tries=60
  while ((tries-- > 0)); do
    if [[ $(docker inspect --format '{{.State.Health.Status}}' "$(compose ps -q api)" 2>/dev/null) == healthy ]]; then
      return 0
    fi
    sleep 3
  done
  return 1
}

# ---------------------------------------------------------------- actions

cmd_install() {
  need_root
  local domain="" token="" owner="" channel=stable version=""
  while (($#)); do
    case $1 in
      --domain) domain=$2; shift 2 ;;
      --token) token=$2; shift 2 ;;
      --owner) owner=$2; shift 2 ;;
      --channel) channel=$2; shift 2 ;;
      --version) version=$2; shift 2 ;;
      *) die "unknown option $1" ;;
    esac
  done
  [[ -f $ENV_FILE ]] && die "already installed in $DIR; use update"
  check_system
  install_docker
  say ""
  say "${BOLD}PauruBot setup${RESET}"
  say "${DIM}Point the domain's DNS A record at this server before continuing.${RESET}"
  [[ -n $domain ]] || domain=$(ask "Domain for the panel (e.g. bot.example.com)")
  [[ -n $token ]] || token=$(ask "Telegram bot token from @BotFather")
  [[ -n $owner ]] || owner=$(ask "Your numeric Telegram user ID")
  [[ $token =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "that does not look like a bot token"
  [[ $owner =~ ^[0-9]+$ ]] || die "the Telegram user ID must be a number"

  resolve_target "$channel" "$version"
  fetch_source "$TARGET_REF"
  local passphrase
  passphrase=$(random_secret 36 40)
  umask 077
  cat >"$ENV_FILE" <<EOF
DOMAIN=$domain
PUBLIC_URL=https://$domain
BOT_TOKEN=$token
OWNER_TELEGRAM_IDS=$owner
MASTER_KEY=$(random_secret 64 64)
BACKUP_PASSPHRASE=$passphrase
POSTGRES_PASSWORD=$(random_secret 32 32)
BOT_MODE=polling
TRUST_PROXY_HOPS=1
TIMEZONE=Asia/Tehran
LOG_LEVEL=INFO
PAURUBOT_CHANNEL=$channel
EOF
  chmod 600 "$ENV_FILE"
  pick_base_images
  fetch_image "$TARGET_IMAGE"
  env_set PAURUBOT_IMAGE "$PINNED_IMAGE"
  env_set PAURUBOT_VERSION "$TARGET_REF"
  compose up -d --remove-orphans
  link_cli
  if wait_healthy; then
    ok "PauruBot is running"
  else
    warn "the API is not healthy yet; check: pauru logs api"
  fi
  say ""
  say "${BOLD}Keep this backup passphrase somewhere safe (not on this server):${RESET}"
  say "  $passphrase"
  say "${DIM}Without it no backup can be restored.${RESET}"
  say ""
  show_setup_link
}

cmd_update() {
  need_root
  local channel version=""
  channel=$(env_get PAURUBOT_CHANNEL)
  channel=${channel:-stable}
  while (($#)); do
    case $1 in
      --channel) channel=$2; shift 2 ;;
      --version) version=$2; shift 2 ;;
      *) die "unknown option $1" ;;
    esac
  done
  [[ -f $ENV_FILE ]] || die "not installed; run install first"
  resolve_target "$channel" "$version"
  say "Updating to ${BOLD}$TARGET_REF${RESET} ($channel)…"
  say "Writing a backup first…"
  compose exec -T worker paurubot backup >/dev/null 2>&1 && ok "backup written" || warn "backup before update failed"
  local previous
  previous=$(env_get PAURUBOT_IMAGE)
  fetch_source "$TARGET_REF"
  fetch_image "$TARGET_IMAGE"
  env_set PAURUBOT_PREVIOUS_IMAGE "$previous"
  env_set PAURUBOT_IMAGE "$PINNED_IMAGE"
  env_set PAURUBOT_VERSION "$TARGET_REF"
  env_set PAURUBOT_CHANNEL "$channel"
  # Migrations run with the new image before any new process starts (the migrate service).
  compose up -d --remove-orphans
  if wait_healthy; then
    ok "updated to $TARGET_REF"
  else
    warn "the new version is not healthy; run: pauru rollback"
  fi
}

cmd_rollback() {
  need_root
  local previous
  previous=$(env_get PAURUBOT_PREVIOUS_IMAGE)
  [[ -n $previous ]] || die "no previous version recorded"
  warn "rolling back to $previous; database migrations are not reversed"
  env_set PAURUBOT_PREVIOUS_IMAGE "$(env_get PAURUBOT_IMAGE)"
  env_set PAURUBOT_IMAGE "$previous"
  compose up -d --remove-orphans
  wait_healthy && ok "rolled back" || warn "still not healthy; check: pauru logs api"
}

cmd_status() {
  compose ps
  say ""
  compose exec -T api paurubot doctor || true
  say ""
  say "version: $(env_get PAURUBOT_VERSION)  channel: $(env_get PAURUBOT_CHANNEL)"
}

cmd_logs() { compose logs --tail=200 -f "${1:-}"; }

cmd_restart() {
  need_root
  compose restart api bot worker
  ok "restarted"
}

cmd_backup() {
  need_root
  compose exec -T worker paurubot backup --send
}

cmd_restore() {
  need_root
  local file=${1:-}
  [[ -n $file && -f $file ]] || die "usage: pauru restore /path/to/paurubot-....pbak"
  confirm "This replaces the current database with $(basename "$file"). Continue?" || exit 1
  local passphrase name output key
  passphrase=$(env_get BACKUP_PASSPHRASE)
  if ! confirm "Was this backup made with this server's BACKUP_PASSPHRASE?"; then
    read -r -s -p "Backup passphrase: " passphrase </dev/tty
    say ""
  fi
  name=$(basename "$file")
  compose stop api bot worker
  compose run --rm --no-deps -T -v "$(realpath "$file"):/restore/$name:ro" -e RESTORE_PASSPHRASE="$passphrase" \
    api restore "/restore/$name" --yes --passphrase-env RESTORE_PASSPHRASE --print-master-key \
    | tee /dev/stderr >"$DIR/.restore.log" || {
    compose up -d
    die "restore failed; the services were started again on the old data"
  }
  output=$(cat "$DIR/.restore.log")
  rm -f "$DIR/.restore.log"
  key=$(printf '%s\n' "$output" | sed -n 's/^MASTER_KEY=//p' | tail -n1)
  if [[ -n $key && $key != "$(env_get MASTER_KEY)" ]]; then
    env_set MASTER_KEY "$key"
    ok "MASTER_KEY taken from the backup"
  fi
  compose up -d
  wait_healthy && ok "restored and running" || warn "check: pauru logs api"
}

show_setup_link() {
  local link
  link=$(compose logs api 2>/dev/null | grep -o 'https\?://[^ ]*/admin/setup?token=[A-Za-z0-9_-]*' | tail -n1 || true)
  if [[ -z $link ]]; then
    link=$(compose exec -T api paurubot setup-token 2>/dev/null | tail -n1 || true)
  fi
  if [[ $link == *token=* ]]; then
    say "${BOLD}Open this link to finish setup (valid for 24 hours):${RESET}"
    say "  $link"
  else
    say "Admin panel: https://$(env_get DOMAIN)/admin/"
  fi
}

cmd_uninstall() {
  need_root
  confirm "Remove PauruBot containers? (data volumes are kept)" || exit 1
  compose down
  if confirm "Also delete ALL data, including the database and backups? This cannot be undone"; then
    compose down -v
    rm -rf "$DIR"
    rm -f /usr/local/bin/pauru
  fi
  ok "uninstalled"
}

cmd_edit() {
  need_root
  "${EDITOR:-nano}" "$ENV_FILE"
  compose up -d
}

link_cli() {
  install -m 755 "$DIR/install.sh" /usr/local/bin/pauru
}

menu() {
  while true; do
    say ""
    say "${BOLD}PauruBot${RESET} ${DIM}$(env_get PAURUBOT_VERSION)${RESET}"
    say "  1) Install        2) Update         3) Rollback"
    say "  4) Status         5) Logs           6) Restart"
    say "  7) Backup now     8) Restore        9) Edit settings"
    say " 10) Setup link    11) Uninstall      0) Exit"
    local choice
    read -r -p "> " choice </dev/tty || exit 0
    case $choice in
      1) cmd_install ;;
      2) cmd_update ;;
      3) cmd_rollback ;;
      4) cmd_status ;;
      5) cmd_logs "$(ask "Service (api, bot, worker, all)" all | sed 's/^all$//')" ;;
      6) cmd_restart ;;
      7) cmd_backup ;;
      8) cmd_restore "$(ask "Path to the .pbak file")" ;;
      9) cmd_edit ;;
      10) show_setup_link ;;
      11) cmd_uninstall ;;
      0 | q) exit 0 ;;
      *) warn "pick a number from the menu" ;;
    esac
  done
}

main() {
  local action=${1:-menu}
  (($#)) && shift
  case $action in
    menu) menu ;;
    install) cmd_install "$@" ;;
    update) cmd_update "$@" ;;
    rollback) cmd_rollback ;;
    status) cmd_status ;;
    logs) cmd_logs "$@" ;;
    restart) cmd_restart ;;
    backup) cmd_backup ;;
    restore) cmd_restore "$@" ;;
    setup-link) show_setup_link ;;
    edit) cmd_edit ;;
    uninstall) cmd_uninstall ;;
    -h | --help | help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "unknown action $action (try --help)" ;;
  esac
}

main "$@"
