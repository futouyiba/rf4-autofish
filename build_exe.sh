#!/usr/bin/env bash
# 一键构建 Windows 绿色版：构建 onedir 包，再压成可直接分发的 zip。
#
#   cd bot && bash build_exe.sh            # 正式版
#   cd bot && SKIP_ZIP=1 bash build_exe.sh # 只构建不压缩（调试用）
#
# 前置：refs/RussianFishing4Script/.venv 已存在（RF4S 运行时依赖都在里面）。
# 产物：dist/RF4AutoFish/RF4AutoFish.exe  +  dist/RF4AutoFish-vX.Y.Z-win64.zip

set -euo pipefail
cd "$(dirname "$0")"

PY="refs/RussianFishing4Script/.venv/Scripts/python.exe"
if [ ! -x "$PY" ]; then
    PY="vendor/.venv/Scripts/python.exe"
fi
if [ ! -x "$PY" ]; then
    echo "找不到 venv：refs/RussianFishing4Script/.venv 或 vendor/.venv" >&2
    echo "先在 bot/ 下准备 RF4S 快照与 .venv（见 README 快速开始）" >&2
    exit 1
fi

if ! "$PY" -c "import PyInstaller" >/dev/null 2>&1; then
    echo "== 安装 PyInstaller =="
    "$PY" -m pip install --disable-pip-version-check -q "pyinstaller==6.11.1"
fi

VER="$("$PY" -c "import app_paths; print(app_paths.APP_VERSION)")"
echo "== 构建 RF4AutoFish $VER =="
"$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build rf4autofish.spec

echo "== 自检（开发态）=="
"$PY" app_main.py --selftest

if [ "${SKIP_ZIP:-0}" = "1" ]; then
    echo "跳过压缩（SKIP_ZIP=1）"
    exit 0
fi

OUT="dist/RF4AutoFish-v${VER}-win64.zip"
rm -f "$OUT"
echo "== 压缩 -> $OUT =="
"$PY" - "$OUT" <<'PY'
import os
import sys
import zipfile
from pathlib import Path

out = Path(sys.argv[1])
root = Path("dist/RF4AutoFish")
count = 0
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            continue
        zf.write(path, path.relative_to(root.parent).as_posix())
        count += 1
print("打包 %d 个文件，%.1f MB" % (count, out.stat().st_size / 1024 / 1024))
PY

echo "完成：$OUT"
