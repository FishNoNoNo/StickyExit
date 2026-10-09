# -*- coding: utf-8 -*-
"""② 转换模块：把节点列表转成可直接使用的代理 IP。

用户节点池映射 -> sing-box 配置 -> 运行/守护 -> 运行时降级回切 -> 对外 IP:端口
"""
from .configgen import build_config, write_config, generate_and_validate, drop_invalid_nodes
from .core import find_sing_box, validate, validate_outbounds
from .supervisor import Supervisor, SupervisorError
from . import clashapi, health

__all__ = [
    "build_config", "write_config", "generate_and_validate", "drop_invalid_nodes",
    "find_sing_box", "validate", "validate_outbounds", "Supervisor", "SupervisorError",
    "clashapi", "health",
]
