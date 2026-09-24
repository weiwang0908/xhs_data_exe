# -*- coding: utf-8 -*-
"""红薯雷达 —— 程序入口。

职责：环境自检 → 初始化数据库 → 启动桌面界面。
打包后为无控制台的单文件 EXE，因此这里不做任何 print 输出，
所有提示一律走界面弹窗或数据库日志。
"""

from __future__ import annotations

import os
import sys
import traceback


def _fatal(title: str, message: str) -> None:
    """尽最大努力把致命错误显示给用户（没有界面时退化为 stderr）。"""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception:  # noqa: BLE001
        try:
            sys.stderr.write("{}: {}\n".format(title, message))
        except Exception:  # noqa: BLE001
            pass


def _check_env() -> list[str]:
    """检查必需依赖，返回缺失项说明。"""
    problems: list[str] = []
    try:
        import tkinter  # noqa: F401
    except Exception:  # noqa: BLE001
        problems.append("· 当前 Python 缺少 tkinter 图形库，请安装带 tcl/tk 的 Python 3.11/3.12")
    try:
        import sqlite3  # noqa: F401
    except Exception:  # noqa: BLE001
        problems.append("· 标准库 sqlite3 不可用")
    try:
        import PIL  # noqa: F401
    except Exception:  # noqa: BLE001
        problems.append("· 缺少 Pillow（商品主图显示需要），执行：pip install pillow")
    return problems


def main() -> int:
    if sys.version_info < (3, 11):
        _fatal("Python 版本过低",
               "红薯雷达需要 Python 3.11 或 3.12。\n"
               "当前版本：{}".format(sys.version.split()[0]))
        return 1

    problems = _check_env()
    if problems:
        _fatal("运行环境不完整",
               "启动失败，缺少以下组件：\n\n" + "\n".join(problems) +
               "\n\n可在项目目录执行：\npip install -r requirements.txt")
        return 1

    try:
        import config as C
        C.ensure_dirs()

        from desktop import Ctx, RadarApp
        ctx = Ctx()
        app = RadarApp(ctx)
        app.mainloop()
        return 0
    except Exception as exc:  # noqa: BLE001
        detail = traceback.format_exc()
        try:
            import database as D
            import config as C
            db = D.Database()
            db.init()
            db.log("ERROR", "启动", "程序启动失败：{}".format(str(exc)[:200]), detail=detail)
            db.close()
        except Exception:  # noqa: BLE001
            pass
        _fatal("红薯雷达启动失败",
               "程序启动时发生异常：\n\n{}\n\n详细信息：\n{}".format(
                   str(exc)[:300], detail[-1500:]))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
