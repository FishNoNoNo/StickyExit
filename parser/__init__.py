"""① 解析模块：把各种输入变成统一的节点列表。

输入：订阅链接 / Clash YAML 文本 / base64 / 节点 URL 列表
输出：list[dict]，每个 dict = { tag, name, outbound }
      outbound 为 sing-box outbound 结构。
"""
from .fetcher import (
    fetch_nodes,          # 从订阅链接拉取
    parse_clash_yaml,     # Clash YAML 文本
    parse_plain_links,    # 纯文本 URL 列表
    clash_node_to_outbound,  # 单个 Clash 节点 -> sing-box outbound
    normalize_outbound,
)

__all__ = [
    "fetch_nodes", "parse_clash_yaml", "parse_plain_links",
    "clash_node_to_outbound", "normalize_outbound",
]
