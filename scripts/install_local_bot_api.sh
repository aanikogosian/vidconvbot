#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root."
  exit 1
fi

if [[ $# -lt 2 ]]; then
  cat <<'USAGE'
Usage:
  scripts/install_local_bot_api.sh <api_id> <api_hash> [http_port]

Example:
  scripts/install_local_bot_api.sh 123456 abcdef1234567890 8081

This script will:
  1. Install build dependencies.
  2. Clone the official tdlib/telegram-bot-api repository.
  3. Build and install telegram-bot-api to /usr/local/bin/telegram-bot-api.
  4. Create /etc/telegram-bot-api/local.env with your API credentials.
  5. Create and enable systemd service telegram-bot-api.service.

Optional:
  TELEGRAM_BOT_API_BUILD_JOBS=1 ./scripts/install_local_bot_api.sh <api_id> <api_hash> [http_port]

After that, set these values in your bot .env:
  TELEGRAM_BASE_URL=http://127.0.0.1:8081/bot
  TELEGRAM_BASE_FILE_URL=http://127.0.0.1:8081/file/bot

Before switching your bot to the local server, call:
  https://api.telegram.org/bot<YOUR_BOT_TOKEN>/logOut
USAGE
  exit 1
fi

API_ID="$1"
API_HASH="$2"
HTTP_PORT="${3:-8081}"
BUILD_JOBS="${TELEGRAM_BOT_API_BUILD_JOBS:-1}"

SRC_DIR="/opt/telegram-bot-api-src"
BUILD_DIR="${SRC_DIR}/build"
WORK_DIR="/var/lib/telegram-bot-api"
ENV_DIR="/etc/telegram-bot-api"
ENV_FILE="${ENV_DIR}/local.env"
SERVICE_FILE="/etc/systemd/system/telegram-bot-api.service"

echo "[1/7] Installing dependencies..."
apt-get update
apt-get install -y \
  build-essential \
  cmake \
  gperf \
  git \
  libssl-dev \
  zlib1g-dev

echo "[2/7] Cloning/updating official Telegram Bot API server sources..."
if [[ -d "${SRC_DIR}/.git" ]]; then
  git -C "${SRC_DIR}" fetch --all --tags
  git -C "${SRC_DIR}" pull --ff-only
  git -C "${SRC_DIR}" submodule update --init --recursive
else
  git clone --recursive https://github.com/tdlib/telegram-bot-api.git "${SRC_DIR}"
fi

echo "[3/7] Building telegram-bot-api..."
mkdir -p "${BUILD_DIR}"
cmake -S "${SRC_DIR}" -B "${BUILD_DIR}" -DCMAKE_BUILD_TYPE=Release
echo "Using ${BUILD_JOBS} build job(s). Override with TELEGRAM_BOT_API_BUILD_JOBS if needed."
cmake --build "${BUILD_DIR}" --target install -j"${BUILD_JOBS}"

echo "[4/7] Preparing directories..."
mkdir -p "${WORK_DIR}" "${ENV_DIR}"

echo "[5/7] Writing env file..."
cat > "${ENV_FILE}" <<EOF
TELEGRAM_API_ID=${API_ID}
TELEGRAM_API_HASH=${API_HASH}
TELEGRAM_LOCAL_PORT=${HTTP_PORT}
TELEGRAM_LOCAL_DIR=${WORK_DIR}
EOF
chmod 600 "${ENV_FILE}"

echo "[6/7] Writing systemd service..."
cat > "${SERVICE_FILE}" <<'EOF'
[Unit]
Description=Local Telegram Bot API Server
After=network.target

[Service]
Type=simple
EnvironmentFile=/etc/telegram-bot-api/local.env
ExecStart=/usr/local/bin/telegram-bot-api \
  --api-id=${TELEGRAM_API_ID} \
  --api-hash=${TELEGRAM_API_HASH} \
  --local \
  --http-port=${TELEGRAM_LOCAL_PORT} \
  --dir=${TELEGRAM_LOCAL_DIR}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

echo "[7/7] Enabling and starting systemd service..."
systemctl daemon-reload
systemctl enable --now telegram-bot-api

cat <<EOF

Done.

Check service:
  systemctl status telegram-bot-api --no-pager -l
  journalctl -u telegram-bot-api -f

Now update your bot .env with:
  TELEGRAM_BASE_URL=http://127.0.0.1:${HTTP_PORT}/bot
  TELEGRAM_BASE_FILE_URL=http://127.0.0.1:${HTTP_PORT}/file/bot

Important:
  Before switching the bot from cloud Bot API to local Bot API, call:
  https://api.telegram.org/bot<YOUR_BOT_TOKEN>/logOut

Then restart your bot:
  systemctl restart vidconvbot
EOF
