#!/bin/bash
# 做 Mac 的 Readiverse.app：雙擊就用自己的視窗打開（跟雅思練習室一樣），Dock 上顯示 Readiverse。
# 用法：bash tools/make_mac_app.sh [放 App 的資料夾，預設是桌面]
# 程式改了之後重新跑一次就會更新 App。資料（書、筆記、收藏）放在 ~/Readiverse書庫，不會被動到。
set -e
SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="${1:-$HOME/Desktop}"
APP="$DEST/Readiverse.app"
TMP="$(mktemp -d)"
BV="$HOME/.readiverse/build-venv/bin/python"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"

# ---------- 圖示 ----------
ICONSET="$TMP/icon.iconset"; mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$SRC/assets/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s*2)); sips -z $d $d "$SRC/assets/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$TMP/icon.icns"

# ---------- 要放進 App 的程式 ----------
STAGE="$TMP/stage"; mkdir -p "$STAGE/static" "$STAGE/assets"
cp "$SRC"/server.py "$SRC"/rv_dict.py "$SRC"/docling_worker.py "$SRC"/app.py "$STAGE/"
cp "$SRC"/static/index.html "$STAGE/static/"; cp "$SRC"/assets/icon.png "$STAGE/assets/"

# ---------- 方法一：PyInstaller 打包成真正的 App（需要 uv）----------
if [ ! -x "$BV" ] && [ -x "$UV" ]; then
  echo "第一次打包：建立打包用的 Python 3.12 環境…"
  "$UV" venv -q --allow-existing -p 3.12 "$HOME/.readiverse/build-venv"
  "$UV" pip install -q -p "$BV" pywebview pyinstaller
fi
if [ -x "$BV" ] && "$BV" -c "import webview, PyInstaller" 2>/dev/null; then
  echo "用 PyInstaller 打包中…（約 1 分鐘）"
  ( cd "$STAGE" && "$BV" -m PyInstaller --noconfirm --clean --log-level WARN --windowed --name Readiverse \
      --icon "$TMP/icon.icns" --osx-bundle-identifier app.readiverse \
      --distpath "$TMP/dist" --workpath "$TMP/build" --specpath "$TMP" \
      --add-data "$STAGE/static:static" --add-data "$STAGE/assets:assets" --add-data "$STAGE/docling_worker.py:." \
      --hidden-import server --hidden-import rv_dict --paths "$STAGE" "$STAGE/app.py" )
  if [ -d "$TMP/dist/Readiverse.app" ]; then
    /usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString 1.0" "$TMP/dist/Readiverse.app/Contents/Info.plist" 2>/dev/null || true
    rm -rf "$APP"; mv "$TMP/dist/Readiverse.app" "$APP"
    codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
    touch "$APP"; rm -rf "$TMP"
    echo "已做好：$APP"; exit 0
  fi
  echo "PyInstaller 打包失敗，改用簡單版 App。"
fi

# ---------- 方法二：簡單版 App（用電腦上的 python3 執行；Dock 上會顯示 Python）----------
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$TMP/icon.icns" "$APP/Contents/Resources/icon.icns"
cp -R "$STAGE" "$APP/Contents/Resources/app"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Readiverse</string>
  <key>CFBundleDisplayName</key><string>Readiverse</string>
  <key>CFBundleIdentifier</key><string>app.readiverse</string>
  <key>CFBundleExecutable</key><string>Readiverse</string>
  <key>CFBundleIconFile</key><string>icon</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/Readiverse" <<'SH'
#!/bin/bash
SRC="$(cd "$(dirname "$0")/../Resources/app" && pwd)"
mkdir -p "$HOME/.readiverse"; LOG="$HOME/.readiverse/app.log"
cd "$SRC"; PY=$(command -v python3 || echo /usr/bin/python3)
echo "=== $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"
exec "$PY" app.py >> "$LOG" 2>&1
SH
chmod +x "$APP/Contents/MacOS/Readiverse"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
touch "$APP"; rm -rf "$TMP"
echo "已做好（簡單版）：$APP"
