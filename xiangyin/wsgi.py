"""
PaaS WSGI 入口 (Gunicorn / Waitress / Vercel / Zeabur / Render 通用)
===================================================================
用法：
    gunicorn --bind 0.0.0.0:$PORT wsgi:app
或  waitress-serve --listen=0.0.0.0:$PORT wsgi:app
"""

from app import create_app

# Gunicorn / PaaS 平台导入时自动引用这个 callable
app = create_app()

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", "5000")))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False, threaded=True)
