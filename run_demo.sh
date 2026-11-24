#!/usr/bin/env sh
# OppList on synthetic demo data (JoJo Freight). Nothing here reads
# Outlook or any real data: the data folder is ./demo.
#   ./run_demo.sh             serve on http://127.0.0.1:8766
#   PORT=8800 ./run_demo.sh   another port
# Delete demo/opplist.db (or run: python -m opplist demo --out demo) to
# regenerate; the data is relative to the day it is generated.
set -e
cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"
command -v "$PYTHON" >/dev/null 2>&1 || PYTHON=python
export OPPLIST_DATA_DIR=demo
export PORT="${PORT:-8766}"
if [ ! -f demo/opplist.db ]; then
    echo "Generating demo data in demo/ ..."
    "$PYTHON" -m opplist demo --out demo --seed 42
fi
echo "OppList demo on http://127.0.0.1:$PORT   (Ctrl+C to stop)"
exec "$PYTHON" -c "import os; from opplist import logs; from opplist.web import create_app; logs.setup(); create_app().run(host='127.0.0.1', port=int(os.environ['PORT']), use_reloader=False)"
