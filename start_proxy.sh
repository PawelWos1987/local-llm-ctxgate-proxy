#!/bin/bash
set +H
cd /home/user/ctxproxy
export $(grep -v '^#' .env | xargs)
exec python -u proxy/app.py >> proxy.log 2>&1
