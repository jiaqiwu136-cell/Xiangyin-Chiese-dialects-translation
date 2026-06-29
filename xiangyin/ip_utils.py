"""
IP 地址相关工具：客户端真实 IP 提取 + 匿名地理位置解析。
"""

import threading
import time
from typing import Dict, Optional

import requests

from config import CONFIG


# 简单的内存 LRU 缓存（IP -> (location_str, expire_ts)）
_CACHE: Dict[str, tuple] = {}
_CACHE_MAX = 4096
_CACHE_TTL = 3600 * 24  # 1 天
_LOCK = threading.Lock()


_UNKNOWN = "来自 未知地区 的用户"


# ============================================================
# 1. 从 Flask request 里拿到客户端真实 IP
# ============================================================

def get_client_ip(request) -> str:
    """
    优先读 X-Forwarded-For 的第一个（最左）非内网地址；
    否则依次检查 X-Real-IP、True-Client-IP 等；
    最后退回 request.remote_addr。
    """
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        for part in forwarded.split(","):
            ip = part.strip()
            if _is_public_ip(ip):
                return ip
        # 都不是公网地址？取第一个
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    for hdr in ("X-Real-IP", "True-Client-IP", "CF-Connecting-IP"):
        v = request.headers.get(hdr, "").strip()
        if v:
            return v
    return request.remote_addr or "127.0.0.1"


def _is_public_ip(ip: str) -> bool:
    """非常粗略地过滤掉私网 / 回环地址。不用第三方库避免依赖。"""
    if not ip:
        return False
    if ip.startswith("127.") or ip.startswith("10.") or ip == "::1":
        return False
    if ip.startswith("192.168."):
        return False
    if ip.startswith("172."):
        try:
            second = int(ip.split(".")[1])
            if 16 <= second <= 31:
                return False
        except Exception:
            pass
    return True


# ============================================================
# 2. IP -> "来自 XX省/XX市 的用户"
# ============================================================

def resolve_location(ip: str) -> str:
    """
    使用 CONFIG.ip_geo_service 模板解析 IP 地理位置。
    - 未配置服务 -> 返回 未知地区
    - 调用失败 / 超时 -> 返回 未知地区
    - 绝不返回任何可反推 IP 的信息
    """
    if not ip or not CONFIG.ip_geo_service:
        return _UNKNOWN

    # 缓存命中
    with _LOCK:
        cached = _CACHE.get(ip)
    if cached:
        loc, expire = cached
        if expire > time.time():
            return loc

    try:
        url = CONFIG.ip_geo_service.replace("{ip}", ip)
        resp = requests.get(url, timeout=(5, 10))
        if resp.status_code != 200:
            return _cache_return(ip, _UNKNOWN)
        data = resp.json()
    except Exception:
        return _cache_return(ip, _UNKNOWN)

    province = ""
    city = ""

    # 尝试兼容几种常见免费服务：
    # 1) ipapi.co: {"region": "Guangdong", "city": "Guangzhou", "country_name": "China"}
    for k in ("region", "regionName", "province"):
        if isinstance(data.get(k), str) and data[k]:
            province = data[k]
            break
    for k in ("city", "town"):
        if isinstance(data.get(k), str) and data[k]:
            city = data[k]
            break
    # 2) 中文 ip-api.com: {"regionName":"广东省","city":"广州市"}
    country = (data.get("country") or data.get("country_name") or "")
    if not province and not city and isinstance(country, str) and country:
        # 至少有国家（不展示国名，留空让中文匹配下面来）
        pass

    # 如果拿到的是英文省市，这里不翻译（免费服务不稳定；最终展示中文/英文都接受）
    # 但如果是中文服务直接返回中文，那最理想。

    parts = [p for p in (province, city) if p]
    if not parts:
        return _cache_return(ip, _UNKNOWN)
    return _cache_return(ip, f"来自 {'/'.join(parts)} 的用户")


def _cache_return(ip: str, value: str) -> str:
    try:
        with _LOCK:
            # 淘汰策略：超上限就清空一半（简单避免内存涨）
            if len(_CACHE) >= _CACHE_MAX:
                for k in list(_CACHE.keys())[: _CACHE_MAX // 2]:
                    _CACHE.pop(k, None)
            _CACHE[ip] = (value, time.time() + _CACHE_TTL)
    except Exception:
        pass
    return value
