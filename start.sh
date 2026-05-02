#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate

if [ -f .env ]; then
  export $(grep -v '^#' .env | grep -v '^$' | xargs)
fi

python app.py
