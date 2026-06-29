"""
乡音方言翻译平台 - 配置加载模块
================================
负责从环境变量和 .env 文件中加载：
  1. Flask 服务配置
  2. LLM 服务商及模型池（所有已配置的 PROVIDER 会被自动探测）
  3. 四类专用 LLM 的温度参数
  4. IP 地理位置服务配置
  5. SQLite 数据库路径
"""

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv

# 加载 .env 文件（若存在）
load_dotenv()


# ============================================================
# 数据结构
# ============================================================

@dataclass
class LLMProviderConfig:
    """单个 LLM 服务商配置（所有 provider 均走 OpenAI Compatible 协议）。"""
    name: str                           # 配置前缀名，如 DASHSCOPE
    provider: str                       # openai-compatible
    api_key: str
    base_url: str
    models: List[str]                   # 该服务商可用的模型名列表
    default_model: str                  # 默认使用的模型名


@dataclass
class SystemConfig:
    """系统全局配置。"""

    # ---- Flask ----
    flask_env: str
    flask_port: int
    secret_key: str

    # ---- LLM 服务商池 ----
    providers: Dict[str, LLMProviderConfig]  # key 为前缀名，如 DASHSCOPE
    default_provider_name: str

    # ---- 专用 LLM 温度 ----
    origin_temperature: float
    translate_temperatures: Tuple[float, float, float]  # 三种温度
    zhuyin_temperature: float
    culture_temperature: float

    # ---- IP 地理服务 ----
    ip_geo_service: Optional[str]  # URL 模板，含 {ip} 占位

    # ---- 数据库 ----
    database_url: str

    # ---- 便捷方法 ----
    def get_model_pool(self) -> List[Dict[str, str]]:
        """返回前端可选的模型池列表，每项含 id / label / provider / model。"""
        pool = []
        for prefix, cfg in self.providers.items():
            for m in cfg.models:
                pool.append({
                    "id": f"{prefix}/{m}",
                    "label": f"{prefix} · {m}",
                    "provider": prefix,
                    "model": m,
                    "is_default": (prefix == self.default_provider_name
                                   and m == cfg.default_model),
                })
        return pool

    def resolve_model_id(self, model_id: str) -> Tuple[str, LLMProviderConfig]:
        """
        解析前端传入的 model_id（格式 'PREFIX/model_name'）。
        返回 (model_name, provider_config)。
        若格式错误或 provider 不存在，抛出 ValueError。
        """
        if "/" not in model_id:
            # 未指定前缀，走默认 provider
            default_cfg = self.providers[self.default_provider_name]
            if model_id not in default_cfg.models:
                model_id = default_cfg.default_model
            return model_id, default_cfg

        prefix, model = model_id.split("/", 1)
        if prefix not in self.providers:
            raise ValueError(f"未知的服务商前缀: {prefix}")
        cfg = self.providers[prefix]
        if model not in cfg.models:
            model = cfg.default_model  # 兜底
        return model, cfg


# ============================================================
# 配置解析函数
# ============================================================

_PROVIDER_PREFIX_RE = re.compile(r"^([A-Z0-9_]+)_PROVIDER$")


def _discover_providers() -> Dict[str, LLMProviderConfig]:
    """自动发现所有已配置的 LLM 服务商（通过 *_PROVIDER 环境变量）。"""
    providers: Dict[str, LLMProviderConfig] = {}
    for key, value in os.environ.items():
        m = _PROVIDER_PREFIX_RE.match(key)
        if not m:
            continue
        prefix = m.group(1)
        api_key = os.getenv(f"{prefix}_API_KEY", "").strip()
        if not api_key:
            # 缺少 API Key 视为未配置，跳过
            continue
        base_url = os.getenv(f"{prefix}_BASE_URL", "").strip()
        models_raw = os.getenv(f"{prefix}_MODELS", "").strip()
        models = [m.strip() for m in models_raw.split(",") if m.strip()]
        default_model = (
            os.getenv(f"{prefix}_DEFAULT_MODEL", "").strip()
            or (models[0] if models else "")
        )
        if not models or not default_model or not base_url:
            continue  # 配置不完整则忽略
        providers[prefix] = LLMProviderConfig(
            name=prefix,
            provider=value.strip() or "openai-compatible",
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            models=models,
            default_model=default_model,
        )
    return providers


def _parse_temps(env_key: str, default: float) -> float:
    raw = os.getenv(env_key)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _parse_three_temps(env_key: str,
                       default: Tuple[float, float, float]) -> Tuple[float, float, float]:
    raw = os.getenv(env_key)
    if raw is None:
        return default
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != 3:
        return default
    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except ValueError:
        return default


# ============================================================
# 全局配置单例
# ============================================================

def build_config() -> SystemConfig:
    providers = _discover_providers()

    # 默认 provider 优先使用 DEFAULT_PROVIDER 指定的；否则取第一个
    default_name = os.getenv("DEFAULT_PROVIDER", "").strip()
    if default_name not in providers and providers:
        default_name = next(iter(providers.keys()))

    # PaaS 平台统一会注入 PORT 环境变量（Render / Zeabur / Railway / Heroku 等），
    # 优先级高于 FLASK_PORT，避免部署后启动在 5000 导致平台探活失败。
    flask_port = 5000
    _port_from_env = (
        os.getenv("PORT")
        or os.getenv("FLASK_PORT", "5000")
    )
    try:
        flask_port = int(_port_from_env)
    except ValueError:
        pass

    # ---- SQLite 默认路径：PaaS 根目录常为只读 ----
    # 部署平台（尤其 serverless / 容器化 PaaS）通常只有 /tmp 或 /data 可写。
    # 若用户未显式指定 DATABASE_URL，我们按下列优先级兜底到可写目录：
    #   1. DATABASE_URL 环境变量（原样使用）
    #   2. /data/xiangyin.db （若存在可写，如 Railway/Railway 挂载卷）
    #   3. /tmp/xiangyin.db  （几乎所有 Linux PaaS 都有，Vercel serverless 冷启动后会丢）
    #   4. ./xiangyin.db     （本地开发兜底）
    _default_db = "sqlite:///xiangyin.db"
    if not os.getenv("DATABASE_URL"):
        for _candidate in ("/data/xiangyin.db", "/tmp/xiangyin.db"):
            _dir = os.path.dirname(_candidate)
            if _dir and os.path.isdir(_dir) and os.access(_dir, os.W_OK):
                _default_db = f"sqlite:///{_candidate}"
                break

    return SystemConfig(
        flask_env=os.getenv("FLASK_ENV", "production"),
        flask_port=flask_port,
        secret_key=os.getenv("SECRET_KEY", "dev-secret-change-me"),

        providers=providers,
        default_provider_name=default_name or "",

        origin_temperature=_parse_temps("ORIGIN_TEMPERATURE", 0.1),
        translate_temperatures=_parse_three_temps(
            "TRANSLATE_TEMPERATURES", (0.3, 0.7, 1.2)
        ),
        zhuyin_temperature=_parse_temps("ZHUYIN_TEMPERATURE", 0.4),
        culture_temperature=_parse_temps("CULTURE_TEMPERATURE", 0.8),

        ip_geo_service=os.getenv("IP_GEO_SERVICE") or None,
        database_url=os.getenv("DATABASE_URL", _default_db),
    )


CONFIG: SystemConfig = build_config()
