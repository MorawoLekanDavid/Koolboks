#!/usr/bin/env bash
set -e

KEY="$(dirname "$0")/chat_server_key_pair.pem"
HOST="ubuntu@44.192.21.1"
APP_DIR="/home/ubuntu/new_chatbot"

echo "→ Deploying to $HOST..."
ssh -i "$KEY" -o StrictHostKeyChecking=no "$HOST" "
  set -e
  cd $APP_DIR
  echo '→ Pulling latest code...'
  git pull
  echo '→ Restarting containers...'
  docker compose restart api
  echo '✓ Done.'
"
echo "✓ Deployed successfully."
