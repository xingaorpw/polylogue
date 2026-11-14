"""桌面模式：起服务 + 开一个独立窗口，像一个程序而不是一个网页。

为什么不直接开浏览器标签页：标签页是「一个网页」，有地址栏、有收藏夹、
关掉整个浏览器就没了。这里用浏览器的 app 模式开一个**无地址栏的独立窗口**，
任务栏上是自己的图标，关窗口即退出。

为什么还用浏览器内核而不是自绘 UI：这台机器上只有 tkinter（做不出像样的
聊天界面），装 PySide6/pywebview 又要用户先 pip 装几百 MB。用系统已有的
Edge/Chrome 内核，零依赖，渲染质量还是最好的那一档。
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

__all__ = ["find_browser", "run_app"]

# 常见 Chromium 内核浏览器。顺序 = 偏好：Edge 在 Windows 上一定有。
_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
]


def _is_bind_error(exc: OSError) -> bool:
    """这个 OSError 是不是「端口被占/不能用」。

    Windows: WinError 10048 (已占用) / 10013 (权限不允许)。
    POSIX:   errno.EADDRINUSE / EACCES。

    注意 EACCES 两边都可能出现：POSIX 上绑 1024 以下端口是 EACCES，
    写一个没有权限的目录也是 EACCES。区别是文件类错误带 `filename`，
    socket bind 不带 —— 有 filename 就一定是文件问题，不是端口问题。
    """
    if getattr(exc, "filename", None):
        return False
    code = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
    if code in (10048, 10013):
        return True
    if code in (errno.EADDRINUSE, errno.EACCES, errno.EADDRNOTAVAIL):
        return True
    return False


def find_browser() -> str | None:
    for path in _CANDIDATES:
        if path and Path(path).is_file():
            return path
    # 兜底：PATH 里找
    for exe in ("msedge", "chrome", "brave"):
        from shutil import which

        found = which(exe)
        if found:
            return found
    return None


def _wait_ready(url: str, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/info", timeout=1) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            time.sleep(0.2)
    return False


def run_app(
    members: list[str] | None = None,
    *,
    port: int = 8765,
    custom: dict[str, str] | None = None,
    models: dict[str, str] | None = None,
) -> int:
    from .web import ensure_utf8_streams, serve

    ensure_utf8_streams()

    holder: dict[str, str] = {}
    ready = threading.Event()

    def boot() -> None:
        try:
            serve(
                members,
                port=port,
                custom=custom,
                models=models,
                on_ready=lambda url: (holder.__setitem__("url", url), ready.set()),
            )
        except OSError as exc:
            # 只有 bind 失败才是「端口用不了」。~/.polylogue 不可写、
            # 磁盘满也会抛 OSError，把它们都说成端口问题会把人引到
            # 完全错误的方向（换端口没用）。
            if _is_bind_error(exc):
                holder["error"] = (
                    f"端口 {port} 被占用（{exc}）。换个端口：polylogue --app --port 8800"
                )
            else:
                holder["error"] = f"启动失败：{type(exc).__name__}: {exc}"
            ready.set()
        except Exception as exc:  # noqa: BLE001
            # 数据文件坏了（比如某个会话 JSON 字段类型不对）会在
            # on_ready 之前抛非 OSError。以前只捕 OSError，这个线程
            # 直接死掉、ready 永不 set，用户干等 30 秒只看到「启动超时」。
            holder["error"] = f"启动失败：{type(exc).__name__}: {exc}"
            ready.set()

    thread = threading.Thread(target=boot, daemon=True)
    thread.start()

    if not ready.wait(30):
        print("启动超时（30 秒内服务没起来）")
        print("可能是数据目录异常或端口被占，试试：polylogue --app --port 8800")
        return 1

    if "error" in holder:
        print(holder["error"])
        return 1

    url = holder["url"]

    browser = find_browser()
    if not browser:
        print(f"没找到 Edge 或 Chrome，用默认浏览器打开：{url}")
        try:
            os.startfile(url)  # type: ignore[attr-defined]
        except OSError:
            print(f"请手动打开：{url}")
        print("关掉这个窗口（或按 Ctrl+C）即退出。")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0

    # 独立的 user-data-dir：这样窗口是一个独立进程，
    # 用户关掉窗口我们就能感知到并退出，不会留下后台服务。
    profile = Path(tempfile.gettempdir()) / "polylogue-app-profile"
    profile.mkdir(parents=True, exist_ok=True)

    cmd = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--window-size=1360,900",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        # 没有这个参数时，Edge/Chrome 关掉窗口后进程还留着
        # （「启动加速/后台扩展继续运行」），proc.wait() 永远不返回，
        # 服务和端口一起挂着，下次启动就报端口被占。
        "--disable-background-mode",
    ]

    print(f"窗口已打开：{url}")
    print("关掉窗口即退出。")

    proc = subprocess.Popen(cmd)
    try:
        proc.wait()
    except KeyboardInterrupt:
        try:
            proc.terminate()
        except OSError:
            pass
    return 0
