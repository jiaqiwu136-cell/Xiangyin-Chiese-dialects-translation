# -*- coding: utf-8 -*-
"""
RED TEST: 修复 Zeabur 启动崩溃的 2 个根因：
  1) Gunicorn 用启动命令 app:app 时，app.py 里没有全局 `app` 变量 -> AttributeError；
  2) 崩溃时 Zeabur 选的 worker 是 sync（SSE 翻译流不工作，必须 gevent）。
本测试验证：
  - `import app` 后 `hasattr(app_module, 'app')` -> True
  - `app_module.app` 是一个可调用的 Flask 实例（has url_map, has route /health）
  - gunicorn 启动时 `-k gevent` 实际可从 Procfile 中解析到（即 Procfile 没有被意外写成 sync）
预期：改之前 FAIL（因为 app.py 里没有 app 全局变量）
"""
import os, sys, importlib, subprocess, inspect

HERE = os.path.dirname(os.path.abspath(__file__))
XY_DIR = os.path.join(HERE)  # assuming test is put under xiangyin/
if not os.path.isfile(os.path.join(XY_DIR, "app.py")):
    # running from repo root
    XY_DIR = os.path.join(HERE, "xiangyin")

sys.path.insert(0, XY_DIR)
errors = []

# Test 1: app module exposes global `app` callable
try:
    import app as app_module
    importlib.reload(app_module)
except Exception as e:
    errors.append(f"[T1] import app failed: {e!r}")
else:
    if not hasattr(app_module, "app"):
        errors.append("[T1] FAIL: app module has no global attribute 'app'")
    else:
        flask_app = getattr(app_module, "app")
        if not callable(flask_app):
            errors.append("[T1] FAIL: app.app is not callable")
        elif not hasattr(flask_app, "url_map"):
            errors.append("[T1] FAIL: app.app doesn't look like Flask instance")
        else:
            rules = {r.rule for r in flask_app.url_map.iter_rules()}
            if "/health" not in rules:
                errors.append(f"[T1] FAIL: /health route not registered; rules={list(rules)[:10]}")
            else:
                print("[T1] PASS: app module exposes Flask 'app' with routes")

# Test 2: wsgi module exposes global `app` callable (Procfile default path)
try:
    import wsgi as wsgi_module
    importlib.reload(wsgi_module)
except Exception as e:
    errors.append(f"[T2] import wsgi failed: {e!r}")
else:
    if not hasattr(wsgi_module, "app"):
        errors.append("[T2] FAIL: wsgi module has no 'app' attribute")
    elif not callable(wsgi_module.app) or not hasattr(wsgi_module.app, "url_map"):
        errors.append("[T2] FAIL: wsgi.app is not a Flask app")
    else:
        print("[T2] PASS: wsgi module exposes Flask 'app'")

# Test 3: Procfile uses gevent worker (not sync)
procfile_path = os.path.join(XY_DIR, "Procfile")
if os.path.isfile(procfile_path):
    with open(procfile_path, "r", encoding="utf-8") as f:
        content = f.read()
    if "gevent" not in content:
        errors.append(f"[T3] FAIL: Procfile missing -k gevent -> {content.strip()}")
    elif "wsgi:app" not in content and "app:app" not in content:
        errors.append(f"[T3] FAIL: Procfile has no gunicorn entry point -> {content.strip()}")
    else:
        print(f"[T3] PASS: Procfile gunicorn command present: {content.strip()}")
else:
    errors.append(f"[T3] FAIL: Procfile not found at {procfile_path}")

# Test 4: sanity - env loading doesn't break import of create_app
try:
    from app import create_app  # noqa: F401
    print("[T4] PASS: create_app factory importable")
except Exception as e:
    errors.append(f"[T4] FAIL: cannot import create_app from app: {e!r}")

# Test 5: /health endpoint responds correctly to a test client request
if "app" not in "".join(errors):
    try:
        with app_module.app.test_client() as c:
            r = c.get("/health")
            if r.status_code != 200:
                errors.append(f"[T5] FAIL: /health status={r.status_code} body={r.data[:200]!r}")
            else:
                d = r.get_json(silent=True) or {}
                if d.get("status") != "ok":
                    errors.append(f"[T5] FAIL: /health body not ok: {d}")
                else:
                    print(f"[T5] PASS: /health returns status=ok providers_count={d.get('providers_count')}")
    except Exception as e:
        errors.append(f"[T5] EXCEPTION: {e!r}")

print("\n" + "=" * 60)
if errors:
    print(f"RED: {len(errors)} FAIL:")
    for e in errors:
        print("  " + e)
    sys.exit(1)
else:
    print("GREEN: All tests PASS")
    sys.exit(0)
