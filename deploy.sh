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
  echo '→ Building new image...'
  sudo docker compose build --no-cache api
  echo '→ Stopping old container...'
  PID=\$(sudo docker inspect --format '{{.State.Pid}}' koolbuy-api 2>/dev/null || true)
  if [ -n \"\$PID\" ] && [ \"\$PID\" != '0' ]; then sudo kill -9 \$PID 2>/dev/null || true; sleep 1; fi
  echo '→ Starting new container...'
  sudo docker compose up -d api
  echo '✓ Done.'
"
echo "✓ Deployed successfully."
