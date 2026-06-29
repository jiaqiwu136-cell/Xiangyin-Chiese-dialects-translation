"""SSE 流验证脚本：按行读事件，解析 data: / event: 字段"""
import json
import sys
import urllib.request as ur
import urllib.error as ue
from urllib.parse import quote

BASE = "http://127.0.0.1:5000"
TIMEOUT = 90


def sse_post(path, body):
    url = BASE + path
    data = json.dumps(body).encode("utf-8")
    req = ur.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )
    status = None
    ctype = None
    events = []
    raw_tail = ""
    try:
        with ur.urlopen(req, timeout=TIMEOUT) as r:
            status = r.status
            ctype = r.headers.get("Content-Type", "")
            buf = b""
            while True:
                chunk = r.read(4096)
                if not chunk:
                    break
                buf += chunk
                # 逐行处理 SSE frame
                while b"\n" in buf:
                    line_bytes, buf = buf.split(b"\n", 1)
                    line = line_bytes.decode("utf-8", errors="replace").rstrip("\r")
                    if not line:
                        continue
                    if line.startswith("data:"):
                        events.append(("data", line[5:].lstrip()))
                    elif line.startswith("event:"):
                        events.append(("event", line[6:].lstrip()))
                    else:
                        events.append(("raw", line))
            raw_tail = buf.decode("utf-8", errors="replace")
    except ue.HTTPError as e:
        status = e.code
        ctype = e.headers.get("Content-Type", "")
        raw_tail = e.read().decode("utf-8", errors="replace")
    return status, ctype, events, raw_tail


def dump(title, status, ctype, events, tail):
    print(f"\n===== {title} =====")
    print(f"status={status}  content-type={ctype}")
    done_here = False
    err_here = None
    data_tokens = 0
    for kind, payload in events:
        if kind == "data":
            if payload.startswith("DONE") or payload == "[DONE]":
                done_here = True
            try:
                obj = json.loads(payload)
                if "error" in obj:
                    err_here = obj["error"]
                if "chunk" in obj:
                    data_tokens += len(str(obj["chunk"]))
                if "text" in obj:
                    data_tokens += len(str(obj["text"]))
            except Exception:
                if not payload.startswith("[DONE]") and payload not in ("DONE", ""):
                    data_tokens += len(payload)
    # 打印前后少量 data 片段
    shown = 0
    for kind, payload in events:
        if kind == "data" and shown < 12:
            print(f"  data: {payload[:200]}")
            shown += 1
    if len(events) > shown:
        print(f"  ... 省略 {len(events) - shown} 条事件")
    print(f"  tail: {tail[:200]}")
    print(f"  done={done_here}  error={err_here}  tokens≈{data_tokens}  events={len(events)}")
    return done_here, err_here


# 1. infer-origin
s, ct, ev, tail = sse_post(
    "/api/translate/infer-origin",
    {"text": "今天吃啥子嘛？莫得事，摆一哈哈儿龙门阵", "model_id": "DEEPSEEK/deepseek-chat"},
)
dump("infer-origin", s, ct, ev, tail)

# 2. d2e
s, ct, ev, tail = sse_post(
    "/api/translate/d2e",
    {
        "text": "今天吃啥子嘛？莫得事，摆一哈哈儿龙门阵",
        "origin": "四川话",
        "model_id": "DEEPSEEK/deepseek-chat",
    },
)
dump("d2e", s, ct, ev, tail)

# 3. e2d
s, ct, ev, tail = sse_post(
    "/api/translate/e2d",
    {
        "text": "Where are you from? I was born and raised in Chengdu.",
        "target_dialect": "四川话",
        "model_id": "DEEPSEEK/deepseek-chat",
    },
)
dump("e2d", s, ct, ev, tail)
