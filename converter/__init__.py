# -*- coding: utf-8 -*-
"""② 转换模块：把节点列表转成可直接使用的代理 IP。

用户节点映射 -> sing-box 配置 -> 运行/守护 -> 对外 IP:端口
"""
from .configgen import build_config, write_config, generate_and_validate
from .core import find_sing_box, validate
from .supervisor import Supervisor, SupervisorError

__all__ = [
    "build_config", "write_config", "generate_and_validate",
    "find_sing_box", "validate", "Supervisor", "SupervisorError",
]
