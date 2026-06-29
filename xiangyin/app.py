"""
乡音方言翻译平台 - Flask 应用入口
================================
功能：中文方言 ↔ 英语 双向翻译（归属地推理 / 三版本英语 / 方言注音 / 风土科普 / 社区反馈）
启动方式：
    1. 复制 .env.example 为 .env 并填入真实配置（API Key 等）
    2. .venv\\Scripts\\python.exe app.py   (Windows)
       或  ./.venv/bin/python app.py       (Linux/macOS)
    3. 启动后观察控制台：打印 "=== LLM Provider 配置 ==="，确认已加载的服务商/模型
       若 providers_count 为 0，请检查 .env 中 LLM_* 或 *PROVIDER / *_API_KEY 配置。
"""

from flask import Flask

import sys
# Windows 控制台默认 GBK，打印 Unicode 字符（如 ↔、· 等）会崩溃；
# 统一切换为 UTF-8 + 错误替换，确保启动日志可输出所有字符。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try: _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception: pass

from config import CONFIG
import db
from routes import register_routes


def _print_llm_config_summary() -> None:
    """启动时打印 LLM 配置摘要（需求：配置大模型日志校验）。"""
    line = lambda *a, **k: print(*a, **k)
    line()
    line("=" * 60)
    line("乡音 XIANG·YIN · 方言 ↔ 英语 翻译平台")
    line("=" * 60)
    line(f"FLASK_ENV        : {CONFIG.flask_env}")
    line(f"服务监听端口    : {CONFIG.flask_port}")
    line(f"SQLite 数据库    : {CONFIG.database_url}")
    line(f"默认服务商       : {CONFIG.default_provider_name or '(未设置)'}")
    line(f"LLM 服务商数     : {len(CONFIG.providers)}")
    if CONFIG.providers:
        for name, p in CONFIG.providers.items():
            mark = "  ← 默认" if name == CONFIG.default_provider_name else ""
            line(f"  · [{name}] provider={p.provider}  base_url={p.base_url}{mark}")
            line(f"      模型池: {', '.join(p.models)}")
            line(f"      默认模型: {p.default_model}")
        pool = CONFIG.get_model_pool()
        line(f"合计可选模型数 : {len(pool)}")
        for m in pool:
            tag = " (默认)" if m.get("is_default") else ""
            line(f"    - {m.get('id')}{tag}")
    else:
        line("  ⚠ 当前未配置任何大模型服务商。")
        line("    请复制 .env.example → .env，填入任意一家：")
        line("      · DASHSCOPE_API_KEY=  (阿里通义)")
        line("      · WENXIN_API_KEY=     (百度文心)")
        line("      · ZHIPU_API_KEY=      (智谱AI)")
        line("      · DEEPSEEK_API_KEY=   (DeepSeek)")
        line("    或任意 OpenAI 兼容的 *PROVIDER / *_API_KEY / *_BASE_URL / *_MODELS 组合。")
    temperature_str = ",".join(str(t) for t in CONFIG.translate_temperatures)
    line(f"翻译三版本温度 : ORIGIN={CONFIG.origin_temperature}  D2E=[{temperature_str}]  E2D={CONFIG.zhuyin_temperature}  CULTURE={CONFIG.culture_temperature}")
    line("=" * 60)
    line()


def create_app() -> Flask:
    app = Flask(
        __name__,
        static_folder="static",
        static_url_path="",
        template_folder="templates",
    )
    app.secret_key = CONFIG.secret_key

    # 数据库初始化（建表 / 请求级连接）
    db.init_db(app)

    # 注册所有业务路由 + 静态首页
    register_routes(app)

    @app.get("/health")
    def health():
        return {"status": "ok", "providers_count": len(CONFIG.providers)}

    # 启动日志：在第一个请求到达前也能看见（gunicorn 下请移步 __main__ 块）
    _print_llm_config_summary()

    return app


# PaaS 平台（Zeabur/Render/Vercel/Heroku 等）常自动用 `gunicorn app:app` 启动
# （而不是 Procfile 的 `wsgi:app`），必须在本模块顶层暴露一个可调用的 app
# 否则会抛：AttributeError: Failed to find attribute 'app' in 'app'.
# 这行必须放在「if __name__ == '__main__'」之前，保证 import app 时就能拿到。
# 注：该变量由工厂函数构造，不会触发开发服务器 run()；不会影响本地 `python app.py`。
app = create_app()


if __name__ == "__main__":
    flask_app = app
    # 说明：
    # 1) use_reloader=False：关闭 Werkzeug 双进程重载，避免导航期间连接被中断
    #    导致浏览器出现 net::ERR_ABORTED（编辑保存自动重载仅在开发者手动调试时需要）；
    # 2) threaded=True：浏览器会并发发起 HTML/CSS/JS/API/FEEDBACK 等多条请求，
    #    单线程 Werkzeug 下并发会排队甚至断连，多线程保证响应稳定；
    # 3) debug 保留跟随 FLASK_ENV，但即使 development 也不开 reloader（同上）。
    flask_app.run(
        host="0.0.0.0",
        port=CONFIG.flask_port,
        debug=(CONFIG.flask_env == "development"),
        use_reloader=False,
        threaded=True,
    )
