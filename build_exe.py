# -*- coding: utf-8 -*-
"""一键打包：PyInstaller onedir + windowed。输出 dist/PowerNetManager/"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE / "power_net_manager.py"
ICON = HERE / "app_icon.ico"
NAME = "PowerNetManager"
DIST = HERE / "dist"
BUILD = HERE / "build"

for d in (DIST / NAME, BUILD / NAME):
    if d.exists():
        try:
            shutil.rmtree(d)
            print("cleaned", d.name)
        except PermissionError as e:
            print(f"[ERROR] 无法清理 {d}：{e}\n请先关闭正在运行的 exe/程序再重试。")
            sys.exit(1)
spec = HERE / f"{NAME}.spec"
if spec.exists():
    spec.unlink()

args = [
    sys.executable, "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--windowed",                       # GUI，无控制台黑窗
    "--onedir",
    "--name", NAME,
    "--distpath", str(DIST),
    "--workpath", str(BUILD),
    "--specpath", str(HERE),
]
if ICON.exists():
    args += ["--icon", str(ICON)]
args.append(str(APP))
print("building:", " ".join(args))
r = subprocess.run(args, cwd=HERE)
print("pyinstaller exit:", r.returncode)
if r.returncode != 0:
    sys.exit(r.returncode)

exe = DIST / NAME / f"{NAME}.exe"
print("exe exists:", exe.exists(), "->", exe)
sys.exit(0 if exe.exists() else 2)
