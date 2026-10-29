#!/bin/bash
# 雙擊即可啟動。關掉這個終端機視窗（或按 Ctrl+C）就會停止 server。
cd "$(dirname "$0")" || exit 1

PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then
  echo "找不到 Python 3，請先安裝：https://www.python.org/downloads/"
  read -r -p "按 Enter 關閉..." _
  exit 1
fi

if ! "$PY" -c "import numpy" 2>/dev/null; then
  echo "缺少 numpy，正在安裝..."
  "$PY" -m pip install numpy || { read -r -p "安裝失敗，按 Enter 關閉..." _; exit 1; }
fi

URL="http://127.0.0.1:8765/dlog_viewer_mem3_preview.html"
( sleep 1.5; open "$URL" ) &   # server 起來後自動開瀏覽器

echo "啟動中... 瀏覽器會自動開啟 $URL"
echo "（要停止：關閉此視窗或按 Ctrl+C）"
exec "$PY" server/dlog_server.py
