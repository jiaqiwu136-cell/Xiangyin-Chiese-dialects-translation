"""RED 测试：SSE Content-Type 必须严格单 charset，避免双 charset 导致 Chromium fetch ABORTED。"""
import json
import sys
import urllib.request as ur
import urllib.error as ue

BASE = "http://127.0.0.1:5000"
passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"[PASS] {name}" + (f"  ({detail[:200]})" if detail else ""))
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail[:400]}")


def http_full(path, method="POST", body=None):
    """返回 status, status_reason, headers_dict, raw_body"""
    url = BASE + path
    data = None
    hdrs = {"Accept": "text/event-stream"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = ur.Request(url, data=data, method=method, headers=hdrs)
    headers = {}
    raw = b""
    status = None
    reason = None
    try:
        with ur.urlopen(req, timeout=30) as r:
            status = r.status
            reason = r.reason
            for k, v in r.headers.items():
                headers.setdefault(k.lower(), v)
            raw = r.read()
    except ue.HTTPError as e:
        status = e.code
        reason = e.reason
        for k, v in e.headers.items():
            headers.setdefault(k.lower(), v)
        raw = e.read()
    return status, reason, headers, raw


# ========= RED test 1: Content-Type 不应包含重复 charset =========
paths_bodies = [
    ("SSE infer-origin",
     "/api/translate/infer-origin",
     {"text": "今天吃啥子", "model_id": "DEEPSEEK/deepseek-chat"}),
    ("SSE d2e",
     "/api/translate/d2e",
     {"text": "今天吃啥子", "origin": "四川话", "model_id": "DEEPSEEK/deepseek-chat"}),
    ("SSE e2d",
     "/api/translate/e2d",
     {"text": "Hi there", "target_dialect": "四川话", "model_id": "DEEPSEEK/deepseek-chat"}),
    ("SSE culture/<region>",
     "/api/culture/%E5%9B%9B%E5%B7%9D%E7%9C%81",
     None),
]

for label, path, body in paths_bodies:
    status, reason, headers, raw = http_full(path, "POST" if body else "GET", body)
    ctype = headers.get("content-type", "")
    charset_count = ctype.lower().count("charset=")
    is_event_stream = "text/event-stream" in ctype.lower()
    check(f"{label}: status 2xx", 200 <= status < 300, f"status={status}")
    check(f"{label}: Content-Type = text/event-stream", is_event_stream, ctype)
    check(f"{label}: Content-Type 无重复 charset（仅 1 次）", charset_count <= 1,
          f"charset_count={charset_count} ctype={ctype}")

print()
print("=" * 60)
print(f"总计：{passed + failed} 项，通过 {passed}，失败 {failed}")
sys.exit(0 if failed == 0 else 1)
