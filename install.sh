#!/usr/bin/env bash
# PauruBot installer and manager.
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/NexusGuide/PauruBot/main/install.sh)
#
# Without arguments it opens a menu. Every action also works non-interactively:
#   install.sh install --domain bot.example.com --token 123:ABC --owner 123456 [--channel stable|beta|dev] [--version v1.2.3] [--https-port 443]
#   install.sh update [--channel stable|beta|dev] [--version v1.2.3] [--restore-backup FILE]
#   install.sh rollback | repair | status | logs [service] | restart | backup | restore FILE | setup-link | uninstall
#   install.sh enable-agent | disable-agent   (let the admin panel request updates and rollbacks)
#
# --https-port publishes the web panel on another HTTPS port (e.g. 8443) when a
# VPN panel already owns 443. Port 80 stays in use: Caddy solves the TLS challenge there.
#
# For a private repository, export PAURUBOT_GITHUB_TOKEN (read access to code and packages) first.

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

# Strip invisible formatting characters (zero-width joiners, bidi marks, BOM)
# and normalise dash look-alikes, so copy-pasted values survive validation.
clean_input() {
  local s=$1 c
  for c in $'\u200b' $'\u200c' $'\u200d' $'\u200e' $'\u200f' $'\u2060' \
    $'\u202a' $'\u202b' $'\u202c' $'\u202d' $'\u202e' $'\u2066' $'\u2067' $'\u2068' $'\u2069' $'\ufeff'; do
    s=${s//"$c"/}
  done
  for c in $'\u2010' $'\u2011' $'\u2012' $'\u2013' $'\u2014' $'\u2015' $'\u2212'; do
    s=${s//"$c"/-}
  done
  s=${s//$'\u00a0'/ }
  s=${s#"${s%%[![:space:]]*}"}
  s=${s%"${s##*[![:space:]]}"}
  printf '%s' "$s"
}

confirm() {
  local reply default=${2:-n}
  if [[ $default == y ]]; then
    read -r -p "$1 [Y/n]: " reply </dev/tty || true
    reply=${reply:-y}
  else
    read -r -p "$1 [y/N]: " reply </dev/tty || true
  fi
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
    warn "Docker's own repository is unreachable; trying the distribution packages"
    # A half-finished get.docker.com run leaves its repository behind, which breaks apt-get update.
    rm -f /etc/apt/sources.list.d/docker.list /etc/apt/sources.list.d/docker.sources
    apt-get update -q || true
    apt-get install -yq docker.io docker-compose-v2 ||
      apt-get install -yq docker.io docker-compose-plugin ||
      apt-get install -yq docker.io docker-compose || true
    systemctl enable --now docker >/dev/null 2>&1 || true
  fi
  docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
  ok "Docker is ready"
}

# cosign checks the release image's signature before it runs. A fresh server has none, so take
# sigstore's own release build and accept it only when it matches its published checksum.
COSIGN_URL=${PAURUBOT_COSIGN_URL:-https://github.com/sigstore/cosign/releases/latest/download}
COSIGN_BIN=${PAURUBOT_COSIGN_BIN:-/usr/local/bin/cosign}

install_cosign() {
  command -v cosign >/dev/null && return
  local arch tmp
  case $(uname -m) in
    x86_64 | amd64) arch=amd64 ;;
    aarch64 | arm64) arch=arm64 ;;
    *) die "no cosign build for $(uname -m); install cosign by hand and run this again" ;;
  esac
  say "Installing cosign to verify the release image…"
  tmp=$(mktemp -d)
  if ! curl -fsSL -o "$tmp/cosign-linux-$arch" "$COSIGN_URL/cosign-linux-$arch" ||
    ! curl -fsSL -o "$tmp/checksums.txt" "$COSIGN_URL/cosign_checksums.txt"; then
    rm -rf "$tmp"
    die "cannot download cosign from GitHub; install it by hand and run this again"
  fi
  if ! (cd "$tmp" && grep " cosign-linux-$arch\$" checksums.txt | sha256sum -c --status); then
    rm -rf "$tmp"
    die "the downloaded cosign does not match its published checksum; refusing to use it"
  fi
  install -m 755 "$tmp/cosign-linux-$arch" "$COSIGN_BIN"
  rm -rf "$tmp"
  ok "cosign is installed"
}

check_system() {
  command -v curl >/dev/null || { apt-get update -q && apt-get install -yq curl; }
  command -v openssl >/dev/null || apt-get install -yq openssl
  command -v git >/dev/null || apt-get install -yq git
  install_cosign
  local mem
  mem=$(awk '/MemTotal/ { print int($2 / 1024) }' /proc/meminfo)
  ((mem >= 900)) || warn "only ${mem} MB RAM; 1 GB or more is recommended"
}

# ---------------------------------------------------------------- releases

github_token() {
  local token=${PAURUBOT_GITHUB_TOKEN:-}
  [[ -n $token ]] || token=$(env_get GITHUB_TOKEN)
  printf '%s' "$token"
}

latest_release() {
  local token auth=()
  token=$(github_token)
  [[ -n $token ]] && auth=(-H "Authorization: Bearer $token")
  curl -fsSL "${auth[@]}" "https://api.github.com/repos/$REPO/releases/latest" |
    sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -n1
}

branch_version() {
  local branch=$1 token auth=()
  token=$(github_token)
  [[ -n $token ]] && auth=(-H "Authorization: Bearer $token")
  curl -fsSL "${auth[@]}" -H "Accept: application/vnd.github.raw+json" \
    "https://api.github.com/repos/$REPO/contents/pyproject.toml?ref=$branch" |
    sed -n 's/^version = "\([^"]*\)"$/\1/p' | head -n1
}

latest_for_channel() {
  local channel=${1:-stable}
  case $channel in
    dev) branch_version dev ;;
    beta) branch_version main ;;
    *) latest_release ;;
  esac
}

repo_url() {
  local token
  token=$(github_token)
  if [[ -n $token ]]; then
    printf 'https://x-access-token:%s@github.com/%s.git' "$token" "$REPO"
  else
    printf 'https://github.com/%s.git' "$REPO"
  fi
}

registry_login() {
  local token
  token=$(github_token)
  [[ -n $token ]] || return 0
  printf '%s' "$token" | docker login ghcr.io -u paurubot --password-stdin >/dev/null 2>&1 ||
    warn "could not sign in to ghcr.io with the GitHub token"
}

# Resolve the git ref and the image for a channel or an explicit version.
resolve_target() {
  local channel=$1 version=$2
  if [[ -n $version ]]; then
    TARGET_REF="v${version#v}"
    TARGET_IMAGE="$IMAGE_REPO:${version#v}"
  elif [[ $channel == dev ]]; then
    TARGET_REF=dev
    TARGET_IMAGE="$IMAGE_REPO:dev"
  elif [[ $channel == beta ]]; then
    TARGET_REF=main
    TARGET_IMAGE="$IMAGE_REPO:edge"
  elif [[ $channel == stable ]]; then
    TARGET_REF=$(latest_release || true)
    if [[ -z $TARGET_REF ]]; then
      warn "no stable release published yet; using the beta channel"
      TARGET_REF=main
      TARGET_IMAGE="$IMAGE_REPO:edge"
    else
      TARGET_IMAGE="$IMAGE_REPO:${TARGET_REF#v}"
    fi
  else
    die "unknown channel: $channel (expected stable, beta or dev)"
  fi
}

fetch_source() {
  local ref=$1 fallback=""
  [[ $ref == *-dev* ]] && fallback=dev
  if [[ -d $DIR/.git ]]; then
    git -C "$DIR" remote set-url origin "$(repo_url)"
    if ! git -C "$DIR" fetch --depth 1 origin "$ref" -q 2>/dev/null; then
      if [[ -n $fallback ]]; then
        git -C "$DIR" fetch --depth 1 origin "$fallback" -q
      else
        git -C "$DIR" fetch --depth 1 origin "$ref" -q
      fi
    fi
    git -C "$DIR" -c advice.detachedHead=false checkout -q --force FETCH_HEAD
  else
    mkdir -p "$DIR"
    # A release is a tag, so git would print its long "detached HEAD" notice: not an error.
    if ! git -c advice.detachedHead=false clone -q --depth 1 --branch "$ref" "$(repo_url)" "$DIR.tmp" 2>/dev/null; then
      if [[ -n $fallback ]]; then
        git -c advice.detachedHead=false clone -q --depth 1 --branch "$fallback" "$(repo_url)" "$DIR.tmp"
      else
        git -c advice.detachedHead=false clone -q --depth 1 --branch "$ref" "$(repo_url)" "$DIR.tmp"
      fi
    fi
    cp -a "$DIR.tmp/." "$DIR/"
    rm -rf "$DIR.tmp"
  fi
  # Never leave the access token in .git/config.
  git -C "$DIR" remote set-url origin "https://github.com/$REPO.git"
}

# Refuse an image that this repository's release workflow did not sign.
verify_signature() {
  local digest=$1 identity_repo=${REPO//./\\.}
  cosign verify "$digest" \
    --certificate-identity-regexp "^https://github\.com/$identity_repo/\.github/workflows/release\.yml@refs/(heads/(main|dev)|tags/v[^/]+)$" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com >/dev/null ||
    die "the signature of $digest does not verify; refusing to run it"
}

# The immutable name@sha256:... of an image already pulled, so .env keeps running exactly it.
pinned_digest() {
  docker image inspect --format '{{index .RepoDigests 0}}' "$1" 2>/dev/null || true
}

# Pull, verify and pin the release image. Never silently build an unverified substitute.
fetch_image() {
  local image=$1 digest
  registry_login
  if docker pull -q "$image" >/dev/null 2>&1; then
    digest=$(docker image inspect --format '{{index .RepoDigests 0}}' "$image" 2>/dev/null || true)
    command -v cosign >/dev/null || install_cosign  # an update on a server installed without it
    [[ -n $digest ]] || die "image has no immutable digest; refusing unverified release"
    verify_signature "$digest"
    ok "image signature verified"
    PINNED_IMAGE=${digest:-$image}
  else
    die "cannot pull verified release image $image; refusing silent local-build fallback"
  fi
}

# Docker Hub refuses many Iranian addresses; fall back to the copies published next to our image.
# Both are pinned by digest in .env, and a GHCR copy must carry our release signature (SEC-04).
pick_base_images() {
  local mirror="ghcr.io/nexusguide/paurubot-mirror" pair key image copy digest
  for pair in POSTGRES_IMAGE=postgres:16-alpine REDIS_IMAGE=redis:7-alpine CADDY_IMAGE=caddy:2-alpine; do
    key=${pair%%=*}
    image=${pair#*=}
    if docker pull -q "$image" >/dev/null 2>&1; then
      digest=$(pinned_digest "$image")
      env_set "$key" "${digest:-$image}"
    elif copy="$mirror:${image/:/-}" && docker pull -q "$copy" >/dev/null 2>&1; then
      digest=$(pinned_digest "$copy")
      [[ -n $digest ]] || die "the GHCR copy of $image has no digest; refusing to run it"
      command -v cosign >/dev/null || install_cosign
      verify_signature "$digest"
      env_set "$key" "$digest"
      ok "using the signed GHCR copy of $image"
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
  local domain="" token="" owner="" channel=stable version="" https_port=""
  while (($#)); do
    case $1 in
      --domain) domain=$2; shift 2 ;;
      --token) token=$2; shift 2 ;;
      --owner) owner=$2; shift 2 ;;
      --channel) channel=$2; shift 2 ;;
      --version) version=$2; shift 2 ;;
      --https-port) https_port=$2; shift 2 ;;
      *) die "unknown option $1" ;;
    esac
  done
  https_port=${https_port:-443}
  if [[ ! $https_port =~ ^[0-9]{1,5}$ ]] || ((10#$https_port < 1 || 10#$https_port > 65535)); then
    die "--https-port must be a port number between 1 and 65535"
  fi
  https_port=$((10#$https_port))
  ((https_port != 80)) || die "port 80 stays reserved for the TLS challenge; choose another --https-port"
  if [[ -f $ENV_FILE ]]; then
    # PAURUBOT_IMAGE is written just before the services start: without it nothing ever ran, so
    # an install that stopped half-way (no cosign, no network) can simply start over.
    [[ -z $(env_get PAURUBOT_IMAGE) ]] || die "already installed in $DIR; use update"
    warn "an earlier install stopped before starting anything; starting over"
  fi
  check_system
  install_docker
  say ""
  say "${BOLD}PauruBot setup${RESET}"
  say "${DIM}Point the domain's DNS A record at this server before continuing.${RESET}"
  [[ -n $domain ]] || domain=$(ask "Domain for the panel (e.g. bot.example.com)")
  [[ -n $token ]] || token=$(ask "Telegram bot token from @BotFather")
  [[ -n $owner ]] || owner=$(ask "Your numeric Telegram user ID")
  # Copy-pasted values often carry invisible characters or dash look-alikes.
  domain=$(clean_input "$domain")
  token=$(clean_input "$token")
  owner=$(clean_input "$owner")
  [[ $token =~ ^[0-9]+:[A-Za-z0-9_-]{30,}$ ]] || die "that does not look like a bot token"
  [[ $owner =~ ^[0-9]+$ ]] || die "the Telegram user ID must be a number"
  # The domain lands in .env and the Caddyfile: a plain host name only.
  [[ $domain =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] ||
    die "that does not look like a domain name (e.g. bot.example.com): $(printf '%q' "$domain")"

  resolve_target "$channel" "$version"
  fetch_source "$TARGET_REF"
  local passphrase public_url
  public_url="https://$domain"
  if ((https_port != 443)); then
    public_url="$public_url:$https_port"
    say "${DIM}Serving the panel at $public_url; port 80 stays free for the TLS challenge.${RESET}"
  fi
  passphrase=$(random_secret 36 40)
  umask 077
  cat >"$ENV_FILE" <<EOF
DOMAIN=$domain
PUBLIC_URL=$public_url
HTTPS_PORT=$https_port
BOT_TOKEN=$token
OWNER_TELEGRAM_IDS=$owner
MASTER_KEY=$(random_secret 64 64)
BACKUP_PASSPHRASE=$passphrase
POSTGRES_PASSWORD=$(random_secret 32 32)
POSTGRES_APP_PASSWORD=$(random_secret 32 32)
REDIS_PASSWORD=$(random_secret 32 32)
BOT_MODE=polling
TRUST_PROXY_HOPS=1
TIMEZONE=Asia/Tehran
LOG_LEVEL=INFO
PAURUBOT_CHANNEL=$channel
GITHUB_TOKEN=${PAURUBOT_GITHUB_TOKEN:-}
EOF
  chmod 600 "$ENV_FILE"
  pick_base_images
  fetch_image "$TARGET_IMAGE"
  local installed_ver=$TARGET_REF
  if [[ ($TARGET_REF == dev || $TARGET_REF == main) && -f $DIR/pyproject.toml ]]; then
    installed_ver=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' "$DIR/pyproject.toml" | head -n1)
    installed_ver=${installed_ver:-$TARGET_REF}
  fi
  env_set PAURUBOT_IMAGE "$PINNED_IMAGE"
  env_set PAURUBOT_VERSION "$installed_ver"
  env_set PAURUBOT_REF "$TARGET_REF"
  compose up -d --remove-orphans
  link_cli
  if confirm "Allow updates and rollbacks from the admin panel?" y; then
    cmd_enable_agent
  fi
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
  local channel version="" explicit_backup=""
  local old_channel old_version old_image old_ref
  old_channel=$(env_get PAURUBOT_CHANNEL)
  old_channel=${old_channel:-stable}
  channel=$old_channel
  while (($#)); do
    case $1 in
      --channel) channel=$2; shift 2 ;;
      --version) version=$2; shift 2 ;;
      --restore-backup) explicit_backup=$2; shift 2 ;;
      *) die "unknown option $1" ;;
    esac
  done
  [[ -f $ENV_FILE ]] || die "not installed; run install first"
  [[ -n $(env_get POSTGRES_APP_PASSWORD) ]] || env_set POSTGRES_APP_PASSWORD "$(random_secret 32 32)"
  [[ -n $(env_get REDIS_PASSWORD) ]] || env_set REDIS_PASSWORD "$(random_secret 32 32)"
  resolve_target "$channel" "$version"
  say "Updating to ${BOLD}$TARGET_REF${RESET} ($channel)…"
  say "Writing a backup first…"
  local backup_out backup_file
  backup_out=$(compose exec -T worker paurubot backup 2>/dev/null) || die "backup before update failed; update aborted without changing the installed release"
  # "<path>.pbak (<n> bytes)": the archive a rollback restores if this update migrates the schema.
  backup_file=$(printf '%s\n' "$backup_out" | sed -n 's/^\(\/[^ ]*\.pbak\) ([0-9]* bytes)$/\1/p' | tail -n1)
  [[ -n $backup_file ]] || die "backup before update gave no file; update aborted without changing the installed release"
  ok "backup written: $backup_file"

  old_image=$(env_get PAURUBOT_IMAGE)
  old_version=$(env_get PAURUBOT_VERSION)
  old_ref=$(env_get PAURUBOT_REF)
  if [[ -z $old_ref ]]; then
    case $old_channel in
      dev) old_ref=dev ;;
      beta) old_ref=main ;;
      *) old_ref=${old_version:-main} ;;
    esac
  fi

  # Pull and verify the target image before touching .env, git checkout, or running containers.
  fetch_image "$TARGET_IMAGE"

  # SEC-F22: verify the target release can run on the current schema before switching.
  # Switching from dev back to main (stable/beta) after dev migrations requires restoring a
  # pre-migration backup with the target release, or aborting cleanly without changing anything.
  local state=0 restore=0 restore_backup=""
  PAURUBOT_IMAGE=$PINNED_IMAGE compose run --rm --no-deps -T migrate schema-status || state=$?
  case $state in
    0 | 3 | 5) ;;
    4)
      restore=1
      restore_backup=$explicit_backup
      if [[ -z $restore_backup && $old_channel == dev && $channel != dev ]]; then
        restore_backup=$(env_get PAURUBOT_PRE_DEV_BACKUP)
      fi
      if [[ -z $restore_backup && $PINNED_IMAGE == "$(env_get PAURUBOT_PREVIOUS_IMAGE)" ]]; then
        restore_backup=$(env_get PAURUBOT_PREVIOUS_BACKUP)
      fi
      [[ -n $restore_backup ]] || die "the database was migrated past $TARGET_REF; switching to $channel requires restoring a pre-migration backup (pass --restore-backup FILE or run 'pauru rollback'; see docs/ops/rollback.md)"
      [[ -z ${PAURUBOT_AGENT:-} ]] || die "switching to $TARGET_REF ($channel) requires restoring the pre-migration backup ($restore_backup); run 'pauru update --channel $channel' (or 'pauru rollback') on the server"
      warn "the database was migrated past $TARGET_REF ($channel): switching restores the backup taken before migrating"
      warn "  $restore_backup — everything written since that backup is lost"
      confirm "Restore that backup and switch to $TARGET_REF ($channel)?" || die "update canceled; nothing was changed"
      ;;
    *)
      die "could not compare the database with $TARGET_REF (schema-status exit $state); nothing was changed"
      ;;
  esac

  local prev_backup_before prev_image_before prev_ver_before prev_chan_before prev_ref_before
  prev_backup_before=$(env_get PAURUBOT_PREVIOUS_BACKUP)
  prev_image_before=$(env_get PAURUBOT_PREVIOUS_IMAGE)
  prev_ver_before=$(env_get PAURUBOT_PREVIOUS_VERSION)
  prev_chan_before=$(env_get PAURUBOT_PREVIOUS_CHANNEL)
  prev_ref_before=$(env_get PAURUBOT_PREVIOUS_REF)

  fetch_source "$TARGET_REF"
  local new_ver=$TARGET_REF
  if [[ ($TARGET_REF == dev || $TARGET_REF == main) && -f $DIR/pyproject.toml ]]; then
    new_ver=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' "$DIR/pyproject.toml" | head -n1)
    new_ver=${new_ver:-$TARGET_REF}
  fi

  # Preserve a dedicated snapshot when entering dev from main so repeated dev updates still allow
  # returning cleanly to main (stable/beta).
  if [[ $old_channel != dev && $channel == dev ]]; then
    env_set PAURUBOT_PRE_DEV_BACKUP "$backup_file"
    env_set PAURUBOT_PRE_DEV_IMAGE "$old_image"
    env_set PAURUBOT_PRE_DEV_VERSION "$old_version"
    env_set PAURUBOT_PRE_DEV_CHANNEL "$old_channel"
    env_set PAURUBOT_PRE_DEV_REF "$old_ref"
  fi

  env_set PAURUBOT_PREVIOUS_BACKUP "$backup_file"
  env_set PAURUBOT_PREVIOUS_IMAGE "$old_image"
  env_set PAURUBOT_PREVIOUS_VERSION "$old_version"
  env_set PAURUBOT_PREVIOUS_CHANNEL "$old_channel"
  env_set PAURUBOT_PREVIOUS_REF "$old_ref"
  env_set PAURUBOT_IMAGE "$PINNED_IMAGE"
  env_set PAURUBOT_VERSION "$new_ver"
  env_set PAURUBOT_CHANNEL "$channel"
  env_set PAURUBOT_REF "$TARGET_REF"

  if ((restore)); then
    compose stop api bot worker
    if ! compose run --rm --no-deps -T migrate restore "$restore_backup" --yes; then
      env_set PAURUBOT_IMAGE "$old_image"
      env_set PAURUBOT_VERSION "$old_version"
      env_set PAURUBOT_CHANNEL "$old_channel"
      env_set PAURUBOT_REF "$old_ref"
      env_set PAURUBOT_PREVIOUS_BACKUP "$prev_backup_before"
      env_set PAURUBOT_PREVIOUS_IMAGE "$prev_image_before"
      env_set PAURUBOT_PREVIOUS_VERSION "$prev_ver_before"
      env_set PAURUBOT_PREVIOUS_CHANNEL "$prev_chan_before"
      env_set PAURUBOT_PREVIOUS_REF "$prev_ref_before"
      if [[ -n $old_ref ]]; then
        fetch_source "$old_ref" || true
      fi
      compose up -d --remove-orphans
      die "restore failed; the database is unchanged and the current release was started again"
    fi
    if [[ $restore_backup == "$(env_get PAURUBOT_PRE_DEV_BACKUP)" ]]; then
      env_set PAURUBOT_PRE_DEV_BACKUP ""
      env_set PAURUBOT_PRE_DEV_IMAGE ""
      env_set PAURUBOT_PRE_DEV_VERSION ""
      env_set PAURUBOT_PRE_DEV_CHANNEL ""
      env_set PAURUBOT_PRE_DEV_REF ""
    fi
  elif [[ $old_channel == dev && $channel != dev ]]; then
    env_set PAURUBOT_PRE_DEV_BACKUP ""
    env_set PAURUBOT_PRE_DEV_IMAGE ""
    env_set PAURUBOT_PRE_DEV_VERSION ""
    env_set PAURUBOT_PRE_DEV_CHANNEL ""
    env_set PAURUBOT_PRE_DEV_REF ""
  fi

  if [[ -f $DIR/install.sh ]] && declare -F link_cli >/dev/null; then
    link_cli
    if [[ -z ${PAURUBOT_AGENT:-} ]] && command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet paurubot-agent 2>/dev/null; then
      systemctl restart paurubot-agent || true
    fi
  fi

  # Migrations run with the new image before any new process starts (the migrate service).
  compose up -d --remove-orphans
  if wait_healthy; then
    ok "updated to $new_ver ($channel)"
  else
    warn "the new version is not healthy; run: pauru rollback"
  fi
}

swap_release() {
  # Exchange the current and previous image/version/channel/ref records (rollback, or undoing one).
  local image version channel ref prev_channel prev_ref
  image=$(env_get PAURUBOT_IMAGE)
  version=$(env_get PAURUBOT_VERSION)
  channel=$(env_get PAURUBOT_CHANNEL)
  ref=$(env_get PAURUBOT_REF)
  prev_channel=$(env_get PAURUBOT_PREVIOUS_CHANNEL)
  prev_ref=$(env_get PAURUBOT_PREVIOUS_REF)
  env_set PAURUBOT_IMAGE "$(env_get PAURUBOT_PREVIOUS_IMAGE)"
  env_set PAURUBOT_VERSION "$(env_get PAURUBOT_PREVIOUS_VERSION)"
  env_set PAURUBOT_PREVIOUS_IMAGE "$image"
  env_set PAURUBOT_PREVIOUS_VERSION "$version"
  if [[ -n $prev_channel || -n $channel ]]; then
    env_set PAURUBOT_CHANNEL "${prev_channel:-$channel}"
    env_set PAURUBOT_PREVIOUS_CHANNEL "$channel"
  fi
  if [[ -n $prev_ref || -n $ref ]]; then
    env_set PAURUBOT_REF "${prev_ref:-$ref}"
    env_set PAURUBOT_PREVIOUS_REF "$ref"
  fi
}

# Migrations only go forward (SEC-F22). An older release must never start on a schema a newer
# release migrated: the previous image itself says whether it can, and when it cannot, the
# rollback restores the backup the update took before migrating, or changes nothing.
cmd_rollback() {
  need_root
  local previous state=0 restore=0 backup
  previous=$(env_get PAURUBOT_PREVIOUS_IMAGE)
  [[ -n $previous ]] || die "no previous version recorded"
  PAURUBOT_IMAGE=$previous compose run --rm --no-deps -T migrate schema-status || state=$?
  case $state in
    0 | 3) ;;
    4) restore=1 ;;
    *) die "could not compare the database with $previous (schema-status exit $state); nothing was changed" ;;
  esac
  if ((restore)); then
    backup=$(env_get PAURUBOT_PREVIOUS_BACKUP)
    [[ -n $backup ]] || die "the database was migrated past $previous and no pre-update backup is recorded; nothing was changed (see docs/ops/rollback.md)"
    [[ -z ${PAURUBOT_AGENT:-} ]] || die "rolling back to $previous means restoring the pre-update backup; run 'pauru rollback' on the server"
    warn "the database was migrated past $previous: rolling back restores the backup taken before the update"
    warn "  $backup — everything written since the update is lost"
    confirm "Restore that backup and roll back?" || die "rollback canceled; nothing was changed"
  else
    warn "rolling back to $previous; the database needs no change"
  fi
  swap_release
  if ((restore)); then
    compose stop api bot worker
    # The previous release restores its own schema; the migrate service holds the owner role.
    if ! compose run --rm --no-deps -T migrate restore "$backup" --yes; then
      swap_release
      compose up -d --remove-orphans
      die "restore failed; the database is unchanged and the current release was started again"
    fi
    env_set PAURUBOT_PREVIOUS_BACKUP ""
    if [[ $backup == "$(env_get PAURUBOT_PRE_DEV_BACKUP)" ]]; then
      env_set PAURUBOT_PRE_DEV_BACKUP ""
      env_set PAURUBOT_PRE_DEV_IMAGE ""
      env_set PAURUBOT_PRE_DEV_VERSION ""
      env_set PAURUBOT_PRE_DEV_CHANNEL ""
      env_set PAURUBOT_PRE_DEV_REF ""
    fi
  fi
  local target_ref
  target_ref=$(env_get PAURUBOT_REF)
  if [[ -z $target_ref ]]; then
    case $(env_get PAURUBOT_CHANNEL) in
      dev) target_ref=dev ;;
      beta) target_ref=main ;;
      *) target_ref=$(env_get PAURUBOT_VERSION) ;;
    esac
  fi
  if [[ -n $target_ref ]] && declare -F fetch_source >/dev/null; then
    fetch_source "$target_ref" || warn "could not check out source ref $target_ref; continuing with existing files"
  fi
  if [[ -f $DIR/install.sh ]] && declare -F link_cli >/dev/null; then
    link_cli
    if [[ -z ${PAURUBOT_AGENT:-} ]] && command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet paurubot-agent 2>/dev/null; then
      systemctl restart paurubot-agent || true
    fi
  fi
  compose up -d --remove-orphans
  wait_healthy && ok "rolled back to $(env_get PAURUBOT_VERSION) ($(env_get PAURUBOT_CHANNEL))" || warn "still not healthy; check: pauru logs api"
}

cmd_status() {
  compose ps
  say ""
  compose exec -T api paurubot doctor || true
  say ""
  say "version: $(env_get PAURUBOT_VERSION)  channel: $(env_get PAURUBOT_CHANNEL)"
  local pre_dev
  pre_dev=$(env_get PAURUBOT_PRE_DEV_BACKUP)
  [[ -n $pre_dev ]] && say "pre-dev backup: $pre_dev"
}

cmd_logs() { compose logs --tail=200 -f "${1:-}"; }

cmd_repair() {
  require_root
  [[ -f $ENV_FILE ]] || die "not installed yet — run: install.sh install"
  local channel version ref image
  channel=$(env_get PAURUBOT_CHANNEL)
  channel=${channel:-stable}
  version=$(env_get PAURUBOT_VERSION)
  ref=$(ref_for_channel "$channel" "$version")
  image=$(env_get PAURUBOT_IMAGE)
  image=${image:-$IMAGE_REPO:${version:-latest}}

  say "Taking a safety backup before repair..."
  cmd_backup || warn "backup failed; continuing with container repair"

  say "Refreshing compose stack from $ref..."
  fetch_repo_files "$ref"
  say "Pulling $image..."
  docker pull "$image"
  compose up -d --force-recreate --remove-orphans
  wait_healthy
  say "${GREEN}Repair complete.${RESET}"
  cmd_status
}

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
  # `-e NAME` without a value passes it from this shell, so the passphrase never shows in `ps`.
  # The migrate service carries the schema owner's credentials; the app roles cannot restore (SEC-F26).
  RESTORE_PASSPHRASE=$passphrase compose run --rm --no-deps -T -v "$(realpath "$file"):/restore/$name:ro" -e RESTORE_PASSPHRASE \
    migrate restore "/restore/$name" --yes --passphrase-env RESTORE_PASSPHRASE --print-master-key \
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
    # PUBLIC_URL carries the port when the panel is not on 443 (--https-port).
    say "Admin panel: $(env_get PUBLIC_URL)/admin/"
  fi
}

cmd_uninstall() {
  [[ -f $AGENT_UNIT ]] && cmd_disable_agent
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

# --- updates from the admin panel (OPS-07) -------------------------------------------------
# The panel never touches Docker. It leaves a request in Redis (reachable only inside the
# compose network) and this host agent, run by systemd, performs it with the normal update
# and rollback actions, then reports back through Redis.

AGENT_UNIT=/etc/systemd/system/paurubot-agent.service
AGENT_LOG=/var/log/paurubot-agent.log

rcli() {
  local pw
  pw=$(env_get REDIS_PASSWORD)
  if [[ -n $pw ]]; then
    compose exec -T -e "REDISCLI_AUTH=$pw" redis redis-cli --no-auth-warning "$@"
  else
    compose exec -T redis redis-cli "$@"
  fi
}

rset() { rcli SET "$1" "$2" EX "${3:-180}" >/dev/null 2>&1 || true; }

agent_report() {
  rset updater:heartbeat "$(date +%s)"
  rset updater:version "$(env_get PAURUBOT_VERSION)"
  rset updater:channel "$(env_get PAURUBOT_CHANNEL)"
  rset updater:previous "$(env_get PAURUBOT_PREVIOUS_VERSION)"
  rset updater:previous_channel "$(env_get PAURUBOT_PREVIOUS_CHANNEL)"
  rset updater:pre_dev_backup "$(env_get PAURUBOT_PRE_DEV_BACKUP)"
}

json_field() { sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p" <<<"$2" | head -n1; }

cmd_agent() {
  need_root
  [[ -f $ENV_FILE ]] || die "not installed"
  local latest_checked=0 request action channel version result cli_before cli_after
  say "PauruBot agent watching for update requests"
  while true; do
    agent_report
    if (($(date +%s) - latest_checked > 3600)); then
      rset updater:latest "$(latest_for_channel "$(env_get PAURUBOT_CHANNEL)" || true)" 7200
      latest_checked=$(date +%s)
    fi
    request=$(rcli GETDEL updater:request 2>/dev/null | tr -d '\r' || true)
    if [[ -n $request && $request != "(nil)" ]]; then
      action=$(json_field action "$request")
      channel=$(json_field channel "$request")
      version=$(json_field version "$request")
      [[ $channel =~ ^(stable|beta|dev)?$ && $version =~ ^(v?[0-9][0-9A-Za-z.-]*)?$ ]] || action="invalid"
      rset updater:state running 7200
      rset updater:last_action "$action ${version:-$channel}" 2592000
      local args=()
      [[ -n $channel ]] && args+=(--channel "$channel")
      [[ -n $version ]] && args+=(--version "$version")
      cli_before=$(sha256sum /usr/local/bin/pauru 2>/dev/null | awk '{print $1}' || true)
      result="done"
      case $action in
        update) (PAURUBOT_AGENT=1 cmd_update "${args[@]}") >"$AGENT_LOG" 2>&1 || result=failed ;;
        rollback) (PAURUBOT_AGENT=1 cmd_rollback) >"$AGENT_LOG" 2>&1 || result=failed ;;
        *)
          echo "unknown action $action" >"$AGENT_LOG"
          result=failed
          ;;
      esac
      grep -qi "not healthy" "$AGENT_LOG" && result=failed
      tail -n 40 "$AGENT_LOG" | sed 's/\x1b\[[0-9;]*m//g' |
        rcli -x SET updater:log >/dev/null 2>&1 || true
      rset updater:state "$result" 2592000
      rset updater:latest "$(latest_for_channel "$(env_get PAURUBOT_CHANNEL)" || true)" 7200
      latest_checked=$(date +%s)
      agent_report
      cli_after=$(sha256sum /usr/local/bin/pauru 2>/dev/null | awk '{print $1}' || true)
      if [[ -n $cli_after && $cli_after != "$cli_before" && -x /usr/local/bin/pauru ]]; then
        exec /usr/local/bin/pauru agent
      fi
    fi
    sleep 15
  done
}

cmd_enable_agent() {
  need_root
  link_cli
  cat >"$AGENT_UNIT" <<UNIT
[Unit]
Description=PauruBot update agent
After=docker.service
Requires=docker.service

[Service]
Environment=PAURUBOT_DIR=$DIR
ExecStart=/usr/local/bin/pauru agent
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now paurubot-agent >/dev/null
  ok "updates from the admin panel are on (systemctl status paurubot-agent)"
}

cmd_disable_agent() {
  need_root
  systemctl disable --now paurubot-agent >/dev/null 2>&1 || true
  rm -f "$AGENT_UNIT"
  systemctl daemon-reload
  ok "updates from the admin panel are off"
}

menu() {
  while true; do
    say ""
    say "${BOLD}PauruBot${RESET} ${DIM}$(env_get PAURUBOT_VERSION)${RESET}"
    say "  1) Install        2) Update         3) Rollback"
    say "  4) Status         5) Logs           6) Restart"
    say "  7) Backup now     8) Restore        9) Edit settings"
    say " 10) Setup link    11) Uninstall     12) Panel updates on"
    say " 13) Repair"
    say "  0) Exit"
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
      12) cmd_enable_agent ;;
      13) cmd_repair ;;
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
    repair) cmd_repair ;;
    status) cmd_status ;;
    logs) cmd_logs "$@" ;;
    restart) cmd_restart ;;
    backup) cmd_backup ;;
    restore) cmd_restore "$@" ;;
    setup-link) show_setup_link ;;
    edit) cmd_edit ;;
    uninstall) cmd_uninstall ;;
    agent) cmd_agent ;;
    enable-agent) cmd_enable_agent ;;
    disable-agent) cmd_disable_agent ;;
    -h | --help | help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' ;;
    *) die "unknown action $action (try --help)" ;;
  esac
}

main "$@"
