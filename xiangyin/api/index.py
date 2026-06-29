"""
Vercel Serverless Function 入口（WSGI -> serverless 适配）
===========================================================
Vercel Python runtime 要求每个 serverless handler 暴露一个 handler(request, context)
或遵循 ASGI/WSGI 的 app。这里使用官方建议模式：直接 import 并 export Flask WSGI app。
Vercel 会自动把所有 HTTP 请求路由进该 WSGI app，因此可以用一个函数承接全部路由。
"""

import sys
import os

# 让 api/ 下的运行时也能 import 到项目根模块
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import create_app  # noqa: E402

app = create_app()
