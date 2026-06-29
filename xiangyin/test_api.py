"""端到端API验证 v2（方言↔英语） —  TDD RED：代码未改前应大量失败"""
import json
import sys
from urllib.parse import quote
import urllib.request as ur
import urllib.error as ue

BASE = "http://127.0.0.1:5000"
passed = 0
failed = 0

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"[PASS] {name}" + (f"  ({detail[:80]})" if detail else ""))
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail[:200]}")

def http(path, method="GET", body=None, headers=None):
    url = BASE + path
    data = None
    hdrs = {"Accept": "application/json"}
    if headers: hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = ur.Request(url, data=data, method=method, headers=hdrs)
    try:
        with ur.urlopen(req, timeout=20) as r:
            ctype = r.headers.get("Content-Type", "")
            raw = r.read().decode("utf-8")
            status = r.status
    except ue.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
        ctype = e.headers.get("Content-Type", "")
    return status, ctype, raw

# ============ 1. 健康 / 首页 / 静态 ============
s, _, raw = http("/health"); check("GET /health 200", s == 200, raw)
s, _, raw = http("/"); check("GET / 200 含「乡音」", s == 200 and "乡" in raw and "XIANG·YIN" in raw)
for fp in ["/style.css", "/app.js"]:
    s, _, r = http(fp); check(f"静态 {fp} 200", s == 200 and len(r) > 100, f"len={len(r)}")
s, _, raw = http("/api/models"); check("GET /api/models 200 pool/list", s == 200 and "providers" in raw and "pool" in raw)
s, _, raw = http("/api/disclaimer")
try:
    data = json.loads(raw); text_ok = "免责" in data.get("text", "")
except Exception:
    text_ok = ("免责" in raw or r"\u514d\u8d23" in raw)
check("GET /api/disclaimer 200", s == 200 and text_ok)

# ============ 2. 翻译接口：新端点 /d2e 和 /e2d（旧端点 /d2m、/m2d 应 404 或 4xx） ============
# 2a) infer-origin 不变（归属地推理）
s, _, raw = http("/api/translate/infer-origin", "POST", {"text": ""})
check("infer-origin 空text 返回400", s == 400, raw[:100])

s, _, raw = http("/api/translate/infer-origin", "POST", {"text": "今天吃啥子"})
check("infer-origin 有provider→200 / 无provider→503 / 调API失败→502", s in (503, 502, 200), f"status={s}")
try: j = json.loads(raw); has_err = s != 200 and isinstance(j.get("error"), str)
except Exception: has_err = False
if s != 200:
    check("infer-origin 非200情况返回 error 字段", has_err, raw[:100])
else:
    check("infer-origin 200 情况（已接入 provider，SSE 流式）", True)

# 2b) 新端点 d2e (dialect → english)
s, _, raw = http("/api/translate/d2e", "POST", {"text": "今天吃啥子", "origin": "四川话"})
check("POST /api/translate/d2e 200/503", s in (503, 502, 200), f"status={s}")
try: j = json.loads(raw); has_err = s != 200 and isinstance(j.get("error"), str)
except Exception: has_err = False
if s != 200:
    check(f"/d2e 非200情况返回 error 字段", has_err, raw[:100])
else:
    check("/d2e 200 情况", True)  # 只有有provider时才会200

# 2c) 新端点 e2d (english → dialect)
s, _, raw = http("/api/translate/e2d", "POST", {"text": "Shall we watch a movie?", "target_dialect": "四川话"})
check("POST /api/translate/e2d 200/503", s in (503, 502, 200), f"status={s}")
try: j = json.loads(raw); has_err = s != 200 and isinstance(j.get("error"), str)
except Exception: has_err = False
if s != 200:
    check(f"/e2d 非200情况返回 error 字段", has_err, raw[:100])
else:
    check("/e2d 200 情况", True)

# 2d) 旧端点 /d2m /m2d 应不存在（404 或 405；Flask static 的 <path:> GET 路由可能让 POST 返回 405，也表示不可用）
for path in ["/api/translate/d2m", "/api/translate/m2d"]:
    s, _, raw = http(path, "POST", {"text": "x", "origin": "四川话"})
    check(f"旧端点 {path} 返回4xx(不可用)", s in (404, 405), f"status={s}")

# ============ 3. 反馈：direction 改为 dialect_to_english / english_to_dialect ============
# 3a) 合法 dialect_to_english
s, _, raw = http("/api/feedbacks", "POST", {
    "direction": "dialect_to_english",
    "source_text": "今天吃啥子嘛",
    "target_text": "What are we eating today?",
    "origin_region": "四川话",
    "suggested_text": "What do you plan to eat today?",
    "model_id": "TEST/model",
    "temperature": 0.7,
})
check("POST /api/feedbacks dialect_to_english 200 ok", s == 200, raw[:180])
j = json.loads(raw) if raw else {}
ok = j.get("ok")
fb_id_d2e = j.get("id") if ok else None
check("dialect_to_english feedback 返回 id 及 submitter_location",
      ok and isinstance(fb_id_d2e, int) and fb_id_d2e > 0
      and isinstance(j.get("item", {}).get("submitter_location"), str), raw[:150])

# 3b) 合法 english_to_dialect
s, _, raw = http("/api/feedbacks", "POST", {
    "direction": "english_to_dialect",
    "source_text": "Good morning, friend!",
    "target_text": "早上好，朋友！",
    "origin_region": "四川话",
    "suggested_text": "哥老倌，早哦！",
})
check("POST /api/feedbacks english_to_dialect 200 ok", s == 200, raw[:180])
j = json.loads(raw) if raw else {}
fb_id_e2d = j.get("id") if j.get("ok") else None
check("english_to_dialect feedback 返回 id>0",
      j.get("ok") and isinstance(fb_id_e2d, int) and fb_id_e2d > 0, raw[:150])

# 3c) 旧 direction dialect_to_mandarin 必须非法（400）
s, _, raw = http("/api/feedbacks", "POST", {"direction": "dialect_to_mandarin", "source_text": "a", "suggested_text": "b"})
check("旧 direction dialect_to_mandarin → 400", s == 400, f"status={s} {raw[:100]}")

# 3d) 旧 direction mandarin_to_dialect 必须非法（400）
s, _, raw = http("/api/feedbacks", "POST", {"direction": "mandarin_to_dialect", "source_text": "a", "suggested_text": "b"})
check("旧 direction mandarin_to_dialect → 400", s == 400, f"status={s} {raw[:100]}")

# 3e) 列表非空
s, _, raw = http("/api/feedbacks")
check("GET /api/feedbacks count>=2", s == 200, raw[:120])
j = json.loads(raw) if raw else {}
items = j.get("items") or []
check("feedbacks列表至少2条（本次提交的两条）", len(items) >= 2, f"count={j.get('count')} len={len(items)}")

# ============ 4. 投票 ============
if fb_id_d2e:
    s, _, raw = http(f"/api/feedbacks/{fb_id_d2e}/vote", "POST", {"vote": "up"})
    check(f"dialect_to_english feedback 投票 up 200", s == 200, raw[:150])
    j = json.loads(raw) if raw else {}
    check("投票返回 ok=true, upvotes, downvotes",
          j.get("ok") is True and isinstance(j.get("upvotes"), int) and isinstance(j.get("downvotes"), int), raw)
    # 重复
    s, _, raw = http(f"/api/feedbacks/{fb_id_d2e}/vote", "POST", {"vote": "up"})
    j2 = json.loads(raw) if raw else {}
    check("同IP重复up → already_same", j2.get("ok") and j2.get("status") == "already_same", raw[:100])
    # 切换
    s, _, raw = http(f"/api/feedbacks/{fb_id_d2e}/vote", "POST", {"vote": "down"})
    j3 = json.loads(raw) if raw else {}
    check("同IP切换为down → changed 且 计数up-1 down+1",
          j3.get("status") == "changed"
          and j3["upvotes"] == j["upvotes"] - 1
          and j3["downvotes"] == j["downvotes"] + 1, raw)

s, _, raw = http("/api/feedbacks/99999999/vote", "POST", {"vote": "up"})
check("投票不存在id → 404", s == 404, f"status={s}")

# ============ 5. 科普 ============
s, ctype, raw = http("/api/culture/" + quote("四川话"))
if s == 200:
    check("/api/culture/<region> 200 SSE或JSON", ("text/event-stream" in ctype) or ("application/json" in ctype), f"ctype={ctype}")
else:
    try: j = json.loads(raw); has_err = isinstance(j.get("error"), str)
    except Exception: has_err = False
    check("无provider时科普返回503 + error字段", s == 503 and has_err, f"status={s} {raw[:120]}")

# ============ 6. ERR_ABORTED 根因相关：favicon / 静态响应一致性 / 并发 ============
def http_full(path, method="GET", body=None, headers=None):
    """返回 (status, ctype, raw, headers_dict, content_len_header, body_len)"""
    url = BASE + path
    data = None; hdrs = {"Accept": "*/*"}
    if headers: hdrs.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = ur.Request(url, data=data, method=method, headers=hdrs)
    try:
        with ur.urlopen(req, timeout=20) as r:
            ctype = r.headers.get("Content-Type", "")
            raw_bytes = r.read()
            raw = raw_bytes.decode("utf-8", errors="replace")
            status = r.status
            hdict = {k.lower(): v for k, v in r.headers.items()}
            return status, ctype, raw, hdict, len(raw_bytes)
    except ue.HTTPError as e:
        raw_bytes = e.read()
        raw = raw_bytes.decode("utf-8", errors="replace")
        status = e.code
        ctype = e.headers.get("Content-Type", "")
        hdict = {k.lower(): v for k, v in e.headers.items()}
        return status, ctype, raw, hdict, len(raw_bytes)

# 6a) favicon 不应 404（避免浏览器额外失败请求产生 ERR_ABORTED 噪音）
s, _, _, _, _ = http_full("/favicon.ico")
check("GET /favicon.ico 2xx（不返回404避免噪音）", 200 <= s < 300, f"status={s}")

# 6a+) 预览环境会注入 /@vite/client HMR 脚本，若返回404会变成 ERR_ABORTED
s, ct, _, _, _ = http_full("/@vite/client")
check("GET /@vite/client 2xx text/javascript（消除预览HMR注入的ABORTED）",
      200 <= s < 300 and "javascript" in ct,
      f"status={s} ctype={ct}")

# 6b) 静态资源 Content-Length 头与实际 body 长度严格一致（否则浏览器会 ERR_ABORTED）
for fp in ["/", "/style.css", "/app.js"]:
    s, ct, raw, h, body_len = http_full(fp)
    cl = h.get("content-length")
    check(f"{fp} Content-Length 头 == body 实际长度（防止解析阶段ABORTED）",
          s == 200 and cl is not None and int(cl) == body_len,
          f"status={s} CL_header={cl} body_len={body_len} len_raw_str={len(raw)}")

# 6c) 静态响应带 ETag / Accept-Ranges（Flask 原生 send_static_file 特征）
_, _, _, h, _ = http_full("/style.css")
check("静态 /style.css 带 ETag 或 Accept-Ranges（Flask原生static避免冲突）",
      ("etag" in h) or ("accept-ranges" in h),
      f"keys={list(h.keys())[:15]}")

# 6d) 并发请求：同一批 6 个请求不出现连接错误 / HTTP 异常（单线程Werkzeug可能卡死）
import concurrent.futures as cf
def _get(p):
    try:
        s, ct, raw, h, bl = http_full(p)
        return (p, s, None)
    except Exception as ex:
        return (p, None, repr(ex))
paths_conc = ["/", "/style.css", "/app.js", "/health", "/api/models", "/api/disclaimer"]
with cf.ThreadPoolExecutor(max_workers=6) as ex:
    results = list(ex.map(_get, paths_conc))
conc_ok = all(status == 200 and err is None for (_, status, err) in results)
check("并发 6 请求全部 200 且无连接错误（单线程会断连ABORTED）", conc_ok,
      f"results={results}")

# ============ 统计 ============
print()
print("=" * 60)
print(f"总计：{passed+failed} 项，通过 {passed}，失败 {failed}")
sys.exit(0 if failed == 0 else 1)
