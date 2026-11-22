#!/bin/bash
# Mac：雙擊打開 Readiverse（看書時保持這個視窗開著）
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3)
if [ -z "$PY" ]; then
  echo "找不到 python3。請先在「終端機」執行：xcode-select --install，裝好後再雙擊一次。"
  read -n1 -r -p "按任意鍵關閉"; exit 1
fi
exec "$PY" server.py "$@"
