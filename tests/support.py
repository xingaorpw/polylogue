"""测试用的小工具。

为什么需要 safe_print：CI 上 stdout 可能是 cp1252（Windows 默认、以及
专门模拟非 UTF-8 环境的那个 job）。直接 print 中文会抛 UnicodeEncodeError，
测试就红在一个跟被测代码无关的地方。量出来的数字要能打出来，但不能因为
打不出来就把测试搞挂。
"""

from __future__ import annotations


def safe_print(*args, **kwargs) -> None:
    """print，但保证不会因为编码问题抛异常。"""
    try:
        print(*args, **kwargs)
    except UnicodeEncodeError:
        text = " ".join(str(a) for a in args)
        try:
            print(text.encode("ascii", "replace").decode("ascii"), **kwargs)
        except Exception:  # noqa: BLE001 - 打印失败绝不能影响测试结果
            pass
