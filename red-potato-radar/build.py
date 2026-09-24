# -*- coding: utf-8 -*-
"""红薯雷达 —— 打包编排脚本。

用法：
    build.bat              # 完整流程：依赖 → 图标 → 打包 → 校验
    build.bat --no-deps     # 跳过依赖安装
    build.bat --icon-only   # 只生成图标

产物：
    dist/红薯雷达.exe      （单文件、无控制台黑框）
    dist/红薯雷达MCP.exe   （只读 MCP stdio 服务）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
BUILD = os.path.join(ROOT, "build")
ASSETS = os.path.join(ROOT, "assets")
PNG = os.path.join(ASSETS, "app-icon.png")
ICO = os.path.join(ASSETS, "app-icon.ico")

MAIN_SPEC = os.path.join(ROOT, "红薯雷达.spec")
MCP_SPEC = os.path.join(ROOT, "红薯雷达MCP.spec")


def log(msg: str) -> None:
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def run(cmd: list[str], **kw) -> int:
    log("  $ " + " ".join('"{}"'.format(c) if " " in c else c for c in cmd))
    return subprocess.call(cmd, cwd=ROOT, **kw)


# --------------------------------------------------------------------------
# 图标
# --------------------------------------------------------------------------

def make_icon() -> None:
    """生成 app-icon.png 与多尺寸 app-icon.ico。

    若用户已自行放置图标文件，则不覆盖（只需把文件命名为 app-icon.png /
    app-icon.ico 放进 assets/ 即可替换成本项目自己的 Logo）。
    """
    os.makedirs(ASSETS, exist_ok=True)
    if os.path.exists(PNG) and os.path.exists(ICO):
        log("[图标] 已存在，跳过生成（如需替换，请直接覆盖 assets/app-icon.png 和 .ico）")
        return
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        log("[图标] 缺少 Pillow，无法生成图标；将使用系统默认图标。")
        return

    S = 512
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角底：绿色渐变感（用同心圆近似）
    d.rounded_rectangle([0, 0, S - 1, S - 1], radius=110, fill=(18, 161, 80, 255))
    for i in range(6):
        r = 210 - i * 30
        d.ellipse([S / 2 - r, S / 2 - r, S / 2 + r, S / 2 + r],
                  fill=(18 + i * 5, 150 + i * 6, 78 + i * 4, 255))
    d.ellipse([96, 96, S - 96, S - 96], fill=(12, 122, 60, 255))

    # 雷达同心圆
    for r in (190, 140, 90, 42):
        d.ellipse([S / 2 - r, S / 2 - r, S / 2 + r, S / 2 + r],
                  outline=(255, 255, 255, 44), width=4)
    # 十字准线
    d.line([S / 2, 46, S / 2, S - 46], fill=(255, 255, 255, 34), width=3)
    d.line([46, S / 2, S - 46, S / 2], fill=(255, 255, 255, 34), width=3)

    # 扫描扇形
    d.pieslice([S / 2 - 190, S / 2 - 190, S / 2 + 190, S / 2 + 190],
               start=-66, end=-14, fill=(255, 255, 255, 58))
    # 扫描线
    d.line([S / 2, S / 2, S / 2 + 182, S / 2 - 78], fill=(255, 255, 255, 235), width=9)
    # 中心点
    d.ellipse([S / 2 - 15, S / 2 - 15, S / 2 + 15, S / 2 + 15],
              fill=(255, 255, 255, 255))

    # 被雷达捕获的“商品”圆点
    d.ellipse([312, 236, 352, 276], fill=(255, 214, 102, 255))
    d.ellipse([228, 350, 258, 380], fill=(255, 214, 102, 220))
    d.ellipse([366, 350, 390, 374], fill=(255, 214, 102, 190))

    img.save(PNG, "PNG")
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)]
    img.save(ICO, "ICO", sizes=sizes)
    log("[图标] 已生成 {} 和 {}".format(PNG, ICO))


# --------------------------------------------------------------------------
# 依赖
# --------------------------------------------------------------------------

def install_deps() -> int:
    log("[依赖] 安装 requirements.txt …")
    rc = run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
              "-r", os.path.join(ROOT, "requirements.txt")])
    if rc != 0:
        return rc
    log("[依赖] 安装 Playwright Chromium（作为 Chrome/Edge 之后的最后兜底）…")
    # 失败不阻断：目标机器有 Chrome 或 Edge 时仍然可用
    run([sys.executable, "-m", "playwright", "install", "chromium"])
    return 0


# --------------------------------------------------------------------------
# 打包
# --------------------------------------------------------------------------

def pyinstaller(spec: str, label: str, clean: bool) -> int:
    log("[打包] {} ← {}".format(label, os.path.basename(spec)))
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm",
           "--distpath", DIST, "--workpath", BUILD]
    if clean:
        cmd.append("--clean")
    cmd.append(spec)
    return run(cmd)


def verify() -> int:
    log("")
    log("=" * 66)
    log("打包产物校验")
    log("=" * 66)
    ok = True
    for name, desc in (("红薯雷达.exe", "主程序（单文件、无控制台黑框）"),
                       ("红薯雷达MCP.exe", "只读 MCP stdio 服务")):
        path = os.path.join(DIST, name)
        if os.path.exists(path):
            size = os.path.getsize(path)
            log("  [√] {}\n      路径：{}\n      大小：{:.1f} MB    {}".format(
                name, path, size / 1024 / 1024, desc))
        else:
            log("  [×] 未找到 {}（{}）".format(name, desc))
            ok = False
    log("")
    if ok:
        log("两个 EXE 已生成在 dist/ 目录，放在同一文件夹即可共用同一个 monitor.db。")
        log("分发时请把以下文件一起拷贝：红薯雷达.exe、红薯雷达MCP.exe")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    icon_only = "--icon-only" in argv
    no_deps = "--no-deps" in argv
    no_clean = "--no-clean" in argv

    log("=" * 66)
    log("红薯雷达 打包脚本")
    log("  项目目录：{}".format(ROOT))
    log("  解释器  ：{}".format(sys.executable))
    log("=" * 66)

    os.makedirs(DIST, exist_ok=True)
    os.makedirs(BUILD, exist_ok=True)

    make_icon()
    if icon_only:
        return 0

    if not no_deps:
        rc = install_deps()
        if rc != 0:
            log("\n[错误] 依赖安装失败，退出码 {}".format(rc))
            return rc

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("\n[错误] 未安装 PyInstaller。请执行：pip install pyinstaller")
        return 1

    rc = pyinstaller(MAIN_SPEC, "红薯雷达.exe", clean=not no_clean)
    if rc != 0:
        log("\n[错误] 主程序打包失败，退出码 {}".format(rc))
        return rc

    rc = pyinstaller(MCP_SPEC, "红薯雷达MCP.exe", clean=False)
    if rc != 0:
        log("\n[错误] MCP 打包失败，退出码 {}".format(rc))
        return rc

    return verify()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
