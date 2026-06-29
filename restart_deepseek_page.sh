#!/usr/bin/env bash
set -euo pipefail

cd /root/autodl-tmp/font_morph_web

pkill -f "uvicorn app:app --host 127.0.0.1 --port 18083" || true
pkill -f "uvicorn local_deepseek_server:app --host 127.0.0.1 --port 8001" || true
sleep 2

nohup /root/miniconda3/bin/python -m uvicorn local_deepseek_server:app --host 127.0.0.1 --port 8001 > /root/autodl-tmp/font_morph_web/local_deepseek.log 2>&1 &

for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8001/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

nohup /root/miniconda3/bin/python -m uvicorn app:app --host 127.0.0.1 --port 18083 > /root/autodl-tmp/font_morph_web/app_18083.log 2>&1 &

sleep 3
ps -ef | grep -E "uvicorn (app:app|local_deepseek_server:app)" | grep -v grep
