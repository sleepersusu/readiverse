#!/bin/bash
# 從程式資料夾直接打開 Readiverse（開發用；平常用桌面的 Readiverse.app 就好）
# 加 --browser 可以改用瀏覽器打開
cd "$(dirname "$0")" || exit 1
PY=$(command -v python3)
if [ -z "$PY" ]; then
  echo "找不到 python3。請先在「終端機」執行：xcode-select --install，裝好後再雙擊一次。"
  read -n1 -r -p "按任意鍵關閉"; exit 1
fi
exec "$PY" app.py "$@"
