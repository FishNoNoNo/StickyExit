"""通过 sing-box 官方 check 命令校验生成的配置。

权威校验入口：所有配置在写入磁盘前都必须通过本模块的 validate()。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import config

logger = logging.getLogger("distributor.core")


def find_sing_box() -> Path:
    """按平台定位 sing-box 可执行文件。"""
    if sys.platform.startswith("win"):
        name, sub = "sing-box.exe", "sing-box-win"
    elif sys.platform.startswith("linux"):
        name, sub = "sing-box", "sing-box-linux"
    elif sys.platform.startswith("darwin"):
        name, sub = "sing-box", "sing-box-mac"
    else:
        raise RuntimeError(f"不支持的系统类型: {sys.platform}")

    exe = config.LIB_DIR / sub / name
    if not exe.exists():
        raise FileNotFoundError(f"找不到 sing-box 可执行文件: {exe}")
    return exe


def validate(cfg: dict) -> None:
    """让 sing-box check 校验配置，失败抛 CalledProcessError。

    校验只读不写；只是临时把配置喂给 check -c，不产生持久文件。
    """
    # 写临时文件而不是动 config.CONFIG_PATH，避免干扰运行中的实例
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        tmp = f.name
    try:
        exe = find_sing_box()
        env = os.environ.copy()
        proc = subprocess.run(
            [str(exe), "check", "-c", tmp],
            capture_output=True, text=True, env=env, timeout=60,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"sing-box check 未通过 (rc={proc.returncode}):\n{proc.stdout}\n{proc.stderr}"
            )
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass