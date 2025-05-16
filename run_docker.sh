#!/bin/sh
set -e

APP_NAME="monta-local/data-multiagents"

docker build \
--no-cache \
-t "$APP_NAME" .

ENV_FILE="${1:-.env.local}"
echo "Using env file: $ENV_FILE"

COMMAND="docker run -p "8080:80" --env-file=$ENV_FILE $APP_NAME"

echo "RUNNING: $COMMAND "
eval "$COMMAND"
