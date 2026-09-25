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
  # No --no-cache: Docker's layer cache already invalidates correctly on
  # file changes (the COPY . . layer, and everything after it, rebuilds
  # whenever any tracked file changes) -- --no-cache instead forces a fully
  # fresh image on every single deploy, and since nothing ever cleaned up
  # the superseded layers, this filled the server's disk to 99% and broke
  # a deploy outright (pip install failing with 'No space left on device').
  sudo docker compose build api
  echo '→ Removing superseded images (keeps what running containers need)...'
  sudo docker image prune -f
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
