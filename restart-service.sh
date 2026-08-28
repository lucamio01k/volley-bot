#!/usr/bin/env bash
set -euo pipefail

service_name="${VOLLEY_BOT_SERVICE:-volley-bot.service}"
sudo systemctl restart "$service_name"
sudo systemctl status "$service_name" --no-pager

