#!/bin/bash

# Report Python failures instead of tee's successful exit status.
set -o pipefail

cd "$(dirname "$0")" || exit 1

echo "=== Starting MidVox ==="

# Optional account workers must never gate the application bot or dashboard.
if [ -n "${TOKEN:-}" ]; then
  export TOKEN="${TOKEN#Bot }"
fi

# Railway changes the public hostname when this project is cloned to another
# account. Always prefer Railway's live hostname over a copied NEXTAUTH_URL so
# Discord OAuth cannot redirect to an older deployment.
if [ -n "${RAILWAY_PUBLIC_DOMAIN:-}" ]; then
  export NEXTAUTH_URL="https://${RAILWAY_PUBLIC_DOMAIN%/}"
  echo "Dashboard OAuth URL: $NEXTAUTH_URL"
fi

# A Discord bot token identifies its application in the token's first segment.
# Use that runtime identity for OAuth and invites so copied Railway variables
# cannot leave the dashboard connected to a previous bot application.
TOKEN_CLIENT_ID="$(python -c 'import base64, os; part=os.environ.get("TOKEN", "").split(".", 1)[0]; print(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)).decode() if part else "")' 2>/dev/null || true)"
if printf '%s' "$TOKEN_CLIENT_ID" | grep -Eq '^[0-9]+$'; then
  if [ -n "${DISCORD_CLIENT_ID:-}" ] && [ "$DISCORD_CLIENT_ID" != "$TOKEN_CLIENT_ID" ]; then
    echo "WARNING: DISCORD_CLIENT_ID did not match TOKEN; using the active bot application ID." >&2
  fi
  export DISCORD_CLIENT_ID="$TOKEN_CLIENT_ID"
  export NEXT_PUBLIC_DISCORD_CLIENT_ID="$TOKEN_CLIENT_ID"
  echo "Discord application ID: $DISCORD_CLIENT_ID"
fi

# Fail before starting long-lived sidecars when Railway is missing required
# runtime configuration. NEXT_PUBLIC_DASHBOARD_API_KEY remains required until
# the dashboard proxy is migrated fully to a private server-side credential.
REQUIRED_ENV=(
  TOKEN OWNER_IDS DASHBOARD_API_KEY NEXT_PUBLIC_DASHBOARD_API_KEY
  NEXTAUTH_SECRET DISCORD_CLIENT_ID DISCORD_CLIENT_SECRET
)
MISSING_ENV=()
for variable in "${REQUIRED_ENV[@]}"; do
  if [ -z "${!variable:-}" ]; then
    MISSING_ENV+=("$variable")
  fi
done
if [ "${#MISSING_ENV[@]}" -gt 0 ]; then
  echo "FATAL: Missing required environment variables: ${MISSING_ENV[*]}" >&2
  exit 1
fi

# Fail deployment immediately when the image is missing required runtimes.
for runtime in python node; do
  if ! command -v "$runtime" >/dev/null 2>&1; then
    echo "FATAL: $runtime is missing. Build from the repository-root Dockerfile." >&2
    exit 1
  fi
done
if [ ! -f dashboard/.next/BUILD_ID ]; then
  echo "FATAL: Dashboard is not built. Build from the repository-root Dockerfile." >&2
  exit 1
fi
if [ ! -f dashboard/node_modules/next/dist/bin/next ]; then
  echo "FATAL: Dashboard runtime dependencies are missing." >&2
  exit 1
fi

export PORT="${PORT:-8080}"
export PYTHONUNBUFFERED=1

export API_ENABLED=true
export API_PORT=5001
ROOT="$(pwd)"

# Run the optional bump worker independently. Its own bounded validation and
# reconnect handling cannot delay the main bot, dashboard, or downloader.
export USER_DESKTOP_READY_FILE=/tmp/midvox-user-desktop-ready
export USER_GATEWAY_READY_FILE=/tmp/midvox-user-gateway-ready
rm -f "$USER_DESKTOP_READY_FILE" "$USER_GATEWAY_READY_FILE"
BUMP_PID=""
# Restore encrypted owner logins before starting the optional account worker.
export COOKIE_PATH=/tmp/midvox-youtube-cookies.json
timeout 20s python /opt/youtube-session/youtube_session_store.py || echo "WARNING: Login restore timed out; continuing startup."
echo "Starting optional account supervisor in the background..."
(
  cd "$ROOT/bot" || exit 1
  while true; do
    python bump_sidecar.py 2>&1 | tee "$ROOT/bump.log"
    sidecar_exit=${PIPESTATUS[0]}
    if [ "$sidecar_exit" = "78" ]; then
      echo "WARNING: Discord account login required; use Admin Panel > Discord login or update USER_TOKEN. Main services remain running." >&2
      sleep 60
    else
      echo "WARNING: Optional account worker exited ($sidecar_exit); retrying in 5s." >&2
      sleep 5
    fi
  done
) &
BUMP_PID=$!

# Railway's datacenter egress is rejected by YouTube even with valid cookies.
# Run a userspace Cloudflare WARP tunnel in this same container and expose an
# HTTP CONNECT proxy only to Cobalt. Discord and the dashboard stay direct.
setup_warp() {
  local warp_dir="/tmp/midvox-warp"
  local proxy_port="33211"
  mkdir -p "$warp_dir" || return 1

  (
    cd "$warp_dir" || exit 1
    if [ ! -s wgcf-profile.conf ]; then
      wgcf register --accept-tos || exit 1
      wgcf generate || exit 1
    fi

    cat > wireproxy.conf <<EOF
WGConfig = $warp_dir/wgcf-profile.conf

[http]
BindAddress = 127.0.0.1:$proxy_port
EOF

    exec wireproxy -c wireproxy.conf
  ) > "$warp_dir/wireproxy.log" 2>&1 &
  WARP_PID=$!

  for i in $(seq 1 12); do
    if python -c "import urllib.request; proxy='http://127.0.0.1:$proxy_port'; opener=urllib.request.build_opener(urllib.request.ProxyHandler({'https': proxy})); trace=opener.open('https://www.cloudflare.com/cdn-cgi/trace', timeout=8).read(); assert b'warp=on' in trace or b'warp=plus' in trace" >/dev/null 2>&1; then
      export COBALT_PROXY_URL="http://127.0.0.1:$proxy_port"
      echo "WARP egress ready for Cobalt on 127.0.0.1:$proxy_port."
      return 0
    fi
    if ! kill -0 "$WARP_PID" 2>/dev/null; then
      break
    fi
    sleep 1
  done

  echo "WARNING: WARP egress failed; Cobalt will use Railway's direct IP." >&2
  tail -n 20 "$warp_dir/wireproxy.log" 2>/dev/null || true
  unset COBALT_PROXY_URL
  return 1
}

# A locally generated PoToken is designed for the same datacenter IP used by
# Cobalt. WARP remains an opt-in fallback, because its shared proxy can accept
# token generation yet fail the later media fetch.
if [ "${COBALT_WARP:-0}" = "1" ]; then
  setup_warp || true
else
  unset COBALT_PROXY_URL
  echo "Cobalt egress: Railway direct IP with matching local YouTube session."
fi

# Generate YouTube's short-lived Proof-of-Origin token locally without account
# cookies or a second browser. The API is loopback-only and never logs tokens.
if [ ! -f /opt/youtube-session/server.py ] || ! command -v bgutil-pot >/dev/null 2>&1; then
  echo "FATAL: YouTube session generator is missing from the image." >&2
  exit 1
fi
if [ -z "${COBALT_PROXY_URL:-}" ]; then
  echo "YouTube session and Cobalt are using matching direct egress."
fi
export YT_SESSION_PORT="${YT_SESSION_PORT:-8081}"
export YT_SESSION_TTL="${YT_SESSION_TTL:-240}"
export YOUTUBE_SESSION_RELOAD_INTERVAL="${YOUTUBE_SESSION_RELOAD_INTERVAL:-300}"
export YOUTUBE_SESSION_SERVER="http://127.0.0.1:${YT_SESSION_PORT}/"
export YOUTUBE_SESSION_INNERTUBE_CLIENT="${YOUTUBE_SESSION_INNERTUBE_CLIENT:-WEB_EMBEDDED}"
echo "Starting local YouTube session generator on port $YT_SESSION_PORT..."
(
  cd /opt/youtube-session || exit 1
  set -o pipefail
  if [ -n "${COBALT_PROXY_URL:-}" ]; then
    export HTTP_PROXY="$COBALT_PROXY_URL"
    export HTTPS_PROXY="$COBALT_PROXY_URL"
    export GLOBAL_AGENT_HTTP_PROXY="$COBALT_PROXY_URL"
    export NO_PROXY="127.0.0.1,localhost"
  fi
  # The Rust minter runs on demand and exits after renewal. Only this small
  # adapter remains resident, leaving memory available for Cobalt downloads.
  COBALT_PROXY_URL="${COBALT_PROXY_URL:-}" \
    python server.py 2>&1 | tee "$ROOT/yt-session.log"
  generator_exit=${PIPESTATUS[0]}
  exit "$generator_exit"
) &
YT_SESSION_PID=$!

# Validate the JSON fields without logging either secret value.
YT_SESSION_READY=0
for i in $(seq 1 90); do
  if python -c "import json, urllib.request; q=urllib.request.Request('${YOUTUBE_SESSION_SERVER}get_pot', method='POST'); d=json.load(urllib.request.urlopen(q, timeout=2)); assert len(d.get('poToken',d.get('potoken',''))) >= 160 and d.get('contentBinding',d.get('visitor_data'))" >/dev/null 2>&1; then
    echo "YouTube session token is ready."
    YT_SESSION_READY=1
    break
  fi
  if ! kill -0 "$YT_SESSION_PID" 2>/dev/null; then
    echo "WARNING: YouTube session generator exited before producing a token; continuing startup." >&2
    tail -n 40 "$ROOT/yt-session.log" 2>/dev/null || true
    break
  fi
  sleep 1
done
if [ "$YT_SESSION_READY" -ne 1 ]; then
  echo "WARNING: YouTube session token is not ready; Cobalt will load it on a later refresh." >&2
  tail -n 40 "$ROOT/yt-session.log" 2>/dev/null || true
fi

# Cobalt runs locally in this same Railway container. Its processing runtime
# stays private and the bot talks to it over loopback; no second Railway
# service or public Cobalt API key is needed.
export COBALT_API_URL="http://127.0.0.1:9000"
if [ ! -f /opt/cobalt/src/cobalt.js ]; then
  echo "FATAL: Cobalt runtime is missing from the image. Railway must build the repository Dockerfile (builder=DOCKERFILE)." >&2
  exit 1
fi

# The owner may sign in through the private, on-demand Railway browser.
# No browser or account password is kept running in the background.
export COOKIE_PATH="/tmp/midvox-youtube-cookies.json"
if [ ! -f "$COOKIE_PATH" ]; then
  (umask 077; printf '{}' > "$COOKIE_PATH")
fi
python /opt/youtube-session/login.py &
YT_LOGIN_PID=$!
echo "YouTube owner login available at /dashboard/youtube-login."
export COBALT_YOUTUBE_CLIENT="${COBALT_YOUTUBE_CLIENT:-IOS}"
echo "Cobalt YouTube client: $COBALT_YOUTUBE_CLIENT"

echo "Starting local Cobalt sidecar on port 9000..."
(
  while true; do
    cd /opt/cobalt || exit 1
    if [ -n "${COBALT_PROXY_URL:-}" ]; then
      export HTTP_PROXY="$COBALT_PROXY_URL"
      export HTTPS_PROXY="$COBALT_PROXY_URL"
      export NO_PROXY="127.0.0.1,localhost"
    fi
    # Player parsing can exceed 192 MiB; keep a bounded allowance within the
    # shared container's 1 GiB budget instead of aborting during extraction.
    NODE_OPTIONS="${COBALT_NODE_OPTIONS:---max-old-space-size=384}" \
      API_URL="${COBALT_PUBLIC_URL:-http://127.0.0.1:9000/}" \
      API_PORT=9000 API_LISTEN_ADDRESS=127.0.0.1 \
      API_INSTANCE_COUNT=1 API_AUTH_REQUIRED=0 \
      RATELIMIT_MAX=120 \
      CUSTOM_INNERTUBE_CLIENT="$COBALT_YOUTUBE_CLIENT" \
      YOUTUBE_SESSION_SERVER="$YOUTUBE_SESSION_SERVER" \
      YOUTUBE_SESSION_INNERTUBE_CLIENT="$YOUTUBE_SESSION_INNERTUBE_CLIENT" \
      bash -c 'echo "$BASHPID" > /tmp/midvox-cobalt.pid; exec node src/cobalt.js' 2>&1 | tee "$ROOT/cobalt.log"
    echo "WARNING: Cobalt exited (code $?). Restarting in 2s..." >&2
    sleep 2
  done
) &
COBALT_PID=$!
for i in $(seq 1 30); do
  if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/', timeout=1)" > /dev/null 2>&1; then
    echo "Cobalt sidecar is ready."
    break
  fi
  if ! kill -0 "$COBALT_PID" 2>/dev/null; then
    echo "FATAL: Cobalt sidecar exited. Log tail:"
    tail -n 30 "$ROOT/cobalt.log" 2>/dev/null || true
    exit 1
  fi
  sleep 1
done
if ! python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/', timeout=2)" > /dev/null 2>&1; then
  echo "FATAL: Cobalt sidecar did not become ready. Log tail:"
  tail -n 30 "$ROOT/cobalt.log" 2>/dev/null || true
  exit 1
fi

if [ -n "${TOKEN:-}" ]; then
  # â”€â”€ Application emoji sync â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
  # Every Discord application owns its emoji IDs. Sync before importing cogs
  # so commands work immediately when the active bot credential changes.
  echo "Syncing MidVox application emojis..."
  (cd "$ROOT/bot" && timeout 180 python sync_emojis_once.py)
  EMOJI_SYNC_EXIT=$?
  if [ "$EMOJI_SYNC_EXIT" -eq 124 ]; then
    echo "WARNING: Emoji sync timed out; commands will use any icons synced so far." >&2
  elif [ "$EMOJI_SYNC_EXIT" -ne 0 ]; then
    echo "WARNING: Emoji sync failed (code $EMOJI_SYNC_EXIT); bot startup will continue." >&2
  fi

  # Run bot in a restart loop so if it crashes the API stays up.
  (
    ROOT="$(pwd)"
    while true; do
      cd "$ROOT/bot" || { echo "FATAL: bot/ directory not found at $ROOT/bot"; exit 1; }
      echo "Starting bot (API on port 5001)..."
      python midvox.py 2>&1 | tee "$ROOT/bot.log"
      bot_exit=${PIPESTATUS[0]}
      if grep -Eq '401 Unauthorized|Improper token has been passed' "$ROOT/bot.log" 2>/dev/null; then
        echo "ERROR: Discord rejected TOKEN. Bot restart loop stopped; replace TOKEN in Railway to bring MidVox online." >&2
        exit 0
      fi
      echo "WARNING: Bot exited (code $bot_exit). Restarting in 2s..." >&2
      cd "$ROOT"
      sleep 2
    done
  ) &
  BOT_PID=$!

  # Wait for bot API to be ready (just check port is open).
  echo "Waiting for bot API on port 5001..."
  for i in $(seq 1 30); do
    if python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5001/health')" > /dev/null 2>&1; then
      echo "Bot API is ready!"
      break
    fi
    echo "  Attempt $i/30..."
    sleep 2
  done
else
  echo "WARNING: MidVox Discord client skipped because no valid bot credential is configured." >&2
  BOT_PID=""
fi

# Start Next.js dashboard on Railway/Render's public PORT.
cd dashboard
echo "Starting dashboard on port $PORT..."
NODE_OPTIONS="${DASHBOARD_NODE_OPTIONS:---max-old-space-size=128}" \
  node node_modules/next/dist/bin/next start -p "$PORT" -H 0.0.0.0 &
DASHBOARD_PID=$!

echo "=== MidVox running ==="
echo "Bot PID: $BOT_PID"
echo "Dashboard PID: $DASHBOARD_PID"
echo "Cobalt PID: $COBALT_PID"
echo "YouTube session PID: $YT_SESSION_PID"
if [ -n "${BUMP_PID:-}" ]; then
  echo "User presence sidecar PID: $BUMP_PID"
fi
if [ -n "${WARP_PID:-}" ]; then
  echo "WARP PID: $WARP_PID"
fi

# Keep the container attached to all top-level services and forward Railway's
# termination signal to the remaining processes during a deploy.
stop_services() {
  for pid in \
    "${COBALT_PID:-}" "${BOT_PID:-}" "${BUMP_PID:-}" \
    "${DASHBOARD_PID:-}" "${YT_SESSION_PID:-}" "${YT_LOGIN_PID:-}" "${WARP_PID:-}"; do
    if [ -n "$pid" ]; then
      kill -TERM "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
}
trap 'stop_services; exit 0' TERM INT

# Keep the container attached to all long-running services. Individual bot and
# sidecar components already have their own restart loops; exiting on the first
# completed background job can incorrectly terminate a healthy deployment
# during process hand-off.
wait
