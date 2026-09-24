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
  echo '→ Stopping old container (graceful SIGTERM, up to 30s)...'
  # Was 'kill -9' on the raw process -- SIGKILL can't be caught or handled,
  # so any customer mid-debounce-wait (up to BOT_RESPONSE_DELAY_SECONDS,
  # default 15s) at the exact moment of a deploy had their reply silently
  # vanish: no error, no fallback, nothing sent, the asyncio.sleep() task
  # just ceased to exist along with the process. Confirmed live -- a real
  # customer's message was received and even correctly processed up to
  # that wait, then never answered, timestamps landing exactly inside a
  # deploy's kill window. 'docker compose stop' sends SIGTERM first, which
  # uvicorn treats as a real graceful-shutdown request (stop accepting new
  # work, let in-flight tasks finish), and only escalates to SIGKILL if
  # the container hasn't exited after the given timeout -- 30s comfortably
  # covers the debounce wait plus the time to actually generate and send.
  sudo docker compose stop -t 30 api
  echo '→ Starting new container...'
  sudo docker compose up -d api
  echo '✓ Done.'
"
echo "✓ Deployed successfully."
