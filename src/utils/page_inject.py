"""渲染 index.html 时注入服务端配置(如 EXTERNAL_LINKS)。

preview.py 与 bridge.py 共用此函数, 保证两个入口(预览/真实模式)
得到一致的页面配置注入结果。
"""

import json
import os

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 占位符 -> config.json 配置路径
PLACEHOLDERS = {
    "__FINGER_LOOKUP_URL__": "EXTERNAL_LINKS.FINGER_LOOKUP_URL",
    "__HOMEWORK_REVIEW_URL__": "EXTERNAL_LINKS.HOMEWORK_REVIEW_URL",
}


def _get_placeholder_values() -> dict:
    """从 config/config.json 读取占位符对应配置值; 缺失时返回空串。"""
    values = {}
    cfg_path = os.path.join(PROJECT_DIR, "config", "config.json")
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        links = cfg.get("EXTERNAL_LINKS") or {}
    except Exception:
        links = {}
    for placeholder, cfg_path_key in PLACEHOLDERS.items():
        key = cfg_path_key.split(".")[-1]
        values[placeholder] = links.get(key, "") or ""
    return values


def render_index_html() -> str:
    """读取 static/index.html 并将占位符替换为配置值。"""
    html_path = os.path.join(PROJECT_DIR, "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    for placeholder, value in _get_placeholder_values().items():
        html = html.replace(placeholder, value)
    return html
