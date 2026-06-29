"""
乡音方言翻译平台 - API 路由
===========================
所有端点集中在本模块，通过 register_routes(app) 挂载到 Flask。

路由清单：
  GET  /                                   -> 首页 static/index.html
  GET  /favicon.ico                        -> 空 204（避免 404 导致浏览器日志噪音）
  GET  /<static file>                      -> Flask 内置 static handler（app.static_folder）
  GET  /health                             -> 健康检查（已在 app.py）

  GET  /api/models                         -> 模型池列表
  GET  /api/disclaimer                     -> 免责声明

  POST /api/translate/infer-origin         -> 归属地推理 (SSE stream)
  POST /api/translate/d2e                  -> 方言→英语 三版本 (SSE stream)
  POST /api/translate/e2d                  -> 英语→方言 带注音 (SSE stream)

  GET  /api/feedbacks                      -> 反馈列表（时间倒序）
  POST /api/feedbacks                      -> 提交反馈
  POST /api/feedbacks/<int:fid>/vote       -> 点赞 / 点踩

  GET  /api/culture/<region>               -> 科普内容：缓存命中则直接 JSON；未命中则 SSE
                                              生成并写入缓存。
"""

import json
import threading
from typing import Any, Dict, Generator, List, Optional

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    request,
    send_from_directory,
    stream_with_context,
)

import db
import llm_service as ls
from config import CONFIG
from constants import DISCLAIMER_TEXT
from ip_utils import get_client_ip, resolve_location


# ============================================================
# SSE 辅助：把 token generator 包成 Flask 可返回的 event-stream
# ============================================================

def _pack_sse(gen: Generator[str, None, None],
              *,
              extra_done: Optional[Dict[str, Any]] = None,
              parse_json: bool = True,
              ) -> Response:
    """
    逐个 token 推 event: token；最后推 event: done，附加完整 parsed JSON。
    若 parse_json=False，则 done 时不尝试 JSON 解析。
    """
    done_payload: Dict[str, Any] = dict(extra_done or {})

    def inner() -> Generator[str, None, None]:
        buf: List[str] = []
        runtime_error: Optional[str] = None
        try:
            for tok in gen:
                buf.append(tok)
                # SSE 格式：一行 data: ...，事件之间空行分隔
                escaped = json.dumps(tok, ensure_ascii=False)
                yield f"event: token\ndata: {escaped}\n\n"
        except Exception as e:
            runtime_error = f"{type(e).__name__}: {e}"
            err_obj = {
                "type": type(e).__name__,
                "message": str(e),
            }
            # 透传常见子分类
            for extra_key in ("status_code", "provider", "model"):
                if hasattr(e, extra_key):
                    err_obj[extra_key] = getattr(e, extra_key)
            yield (
                f"event: error\n"
                f"data: {json.dumps(err_obj, ensure_ascii=False)}\n\n"
            )

        full = "".join(buf)
        done_payload["raw"] = full
        if runtime_error is not None:
            done_payload["error"] = runtime_error
        if parse_json and full:
            try:
                done_payload["parsed"] = ls.parse_loose_json(full)
            except Exception as e:
                done_payload["parse_error"] = f"{type(e).__name__}: {e}"
        yield f"event: done\ndata: {json.dumps(done_payload, ensure_ascii=False)}\n\n"

    return Response(
        stream_with_context(inner()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # 告诉 nginx 不要缓冲
        },
    )


def _require_json() -> Dict[str, Any]:
    """统一的 JSON body 校验。"""
    if not request.is_json:
        raise ValueError("请求必须是 application/json")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


def _error(message: str, status: int = 400) -> Response:
    resp = jsonify({"error": message})
    resp.status_code = status
    return resp


# ============================================================
# 路由注册
# ============================================================

def register_routes(app: Flask) -> None:

    # ----------------------------------------------------------
    # 静态资源：首页 index.html；其他静态文件由 Flask 内置的
    #   static_folder="static" + static_url_path="" 自动提供
    #   （ETag / Accept-Ranges / 条件GET 更规范，避免响应不一致造成 ABORTED）。
    # ----------------------------------------------------------
    @app.get("/")
    def _home():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/favicon.ico")
    def _favicon():
        # 不返回 404，避免浏览器错误日志。204 No Content 对 favicon 是规范行为。
        return Response(status=204, headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/@vite/client")
    def _vite_client():
        """
        Trae 预览环境会向所有本地预览页注入 /@vite/client 的 HMR 脚本引用；
        本项目是传统静态站（非 Vite），若该请求返回 404，浏览器侧
        会把它记为 net::ERR_ABORTED（module script 加载中断）。
        返回一个空的 ES Module 脚本（200 + Content-Type=text/javascript）
        即可消除该报错，不影响任何业务逻辑。
        """
        return Response(
            "/* noop (not a Vite project) */\n",
            status=200,
            headers={
                "Content-Type": "text/javascript; charset=utf-8",
                "Cache-Control": "no-cache",
            },
        )

    # ----------------------------------------------------------
    # 配置类
    # ----------------------------------------------------------
    @app.get("/api/models")
    def _models():
        return jsonify({
            "default_model_id": (
                f"{CONFIG.default_provider_name}/"
                f"{CONFIG.providers[CONFIG.default_provider_name].default_model}"
                if CONFIG.default_provider_name and CONFIG.providers.get(CONFIG.default_provider_name)
                else None
            ),
            "providers": [
                {
                    "name": name,
                    "base_url": p.base_url,
                    "models": p.models,
                    "default_model": p.default_model,
                }
                for name, p in CONFIG.providers.items()
            ],
            "pool": CONFIG.get_model_pool(),
        })

    @app.get("/api/disclaimer")
    def _disclaimer():
        return jsonify({"text": DISCLAIMER_TEXT})

    # ----------------------------------------------------------
    # 翻译流程 - 1. 归属地推理
    # ----------------------------------------------------------
    @app.post("/api/translate/infer-origin")
    def _infer_origin():
        try:
            data = _require_json()
            text = str(data.get("text", "")).strip()
            model_id = data.get("model_id")
            if not text:
                return _error("text 不能为空")
            resolved, gen = ls.infer_origin_stream(text, model_id=model_id)
            return _pack_sse(gen, extra_done={"model_id": resolved})
        except ls.LLMConfigError as e:
            return _error(str(e), 503)
        except ls.LLMAPIError as e:
            return _error(str(e), 502)
        except ValueError as e:
            return _error(str(e))

    # ----------------------------------------------------------
    # 翻译流程 - 2. 方言 -> 英语（三版本）
    # ----------------------------------------------------------
    @app.post("/api/translate/d2e")
    def _d2e():
        try:
            data = _require_json()
            text = str(data.get("text", "")).strip()
            origin = str(data.get("origin", "")).strip()
            model_id = data.get("model_id")
            if not text:
                return _error("text 不能为空")
            if not origin:
                return _error("origin（归属地）不能为空")
            resolved, gen = ls.translate_d2e_stream(
                text, origin, model_id=model_id
            )
            return _pack_sse(gen, extra_done={"model_id": resolved})
        except ls.LLMConfigError as e:
            return _error(str(e), 503)
        except ls.LLMAPIError as e:
            return _error(str(e), 502)
        except ValueError as e:
            return _error(str(e))

    # ----------------------------------------------------------
    # 翻译流程 - 3. 英语 -> 方言（带注音）
    # ----------------------------------------------------------
    @app.post("/api/translate/e2d")
    def _e2d():
        try:
            data = _require_json()
            text = str(data.get("text", "")).strip()
            target = str(data.get("target_dialect", "")).strip()
            model_id = data.get("model_id")
            if not text:
                return _error("text 不能为空")
            if not target:
                return _error("target_dialect（目标方言）不能为空")
            resolved, gen = ls.translate_e2d_stream(
                text, target, model_id=model_id
            )
            return _pack_sse(gen, extra_done={"model_id": resolved})
        except ls.LLMConfigError as e:
            return _error(str(e), 503)
        except ls.LLMAPIError as e:
            return _error(str(e), 502)
        except ValueError as e:
            return _error(str(e))

    # ----------------------------------------------------------
    # 反馈留言板
    # ----------------------------------------------------------
    @app.get("/api/feedbacks")
    def _list_feedbacks():
        try:
            limit = int(request.args.get("limit", 50))
            offset = int(request.args.get("offset", 0))
        except ValueError:
            return _error("limit / offset 必须是整数")
        rows = db.list_feedbacks(limit=limit, offset=offset)
        return jsonify({"items": rows, "count": len(rows)})

    @app.post("/api/feedbacks")
    def _post_feedback():
        try:
            data = _require_json()
            direction = str(data.get("direction", "")).strip()
            if direction not in ("dialect_to_english", "english_to_dialect"):
                return _error("direction 必须是 dialect_to_english 或 english_to_dialect")
            source_text = str(data.get("source_text", "")).strip()
            suggested_text = str(data.get("suggested_text", "")).strip()
            if not source_text or not suggested_text:
                return _error("source_text / suggested_text 不能为空")
            client_ip = get_client_ip(request)
            loc = resolve_location(client_ip)
            fid = db.create_feedback(
                direction=direction,
                source_text=source_text,
                suggested_text=suggested_text,
                client_ip=client_ip,
                target_text=str(data.get("target_text", "") or "") or None,
                origin_region=str(data.get("origin_region", "") or "") or None,
                submitter_location=loc,
                model_id=str(data.get("model_id", "") or "") or None,
                temperature=data.get("temperature"),
            )
            row = db.get_feedback(fid)
            return jsonify({"ok": True, "id": fid, "item": row})
        except ValueError as e:
            return _error(str(e))
        except Exception as e:
            return _error(f"服务器内部错误: {type(e).__name__}", 500)

    @app.post("/api/feedbacks/<int:fid>/vote")
    def _vote(fid: int):
        try:
            data = _require_json()
            vote = str(data.get("vote", "")).strip()
            if vote not in ("up", "down"):
                return _error("vote 必须是 up 或 down")
            client_ip = get_client_ip(request)
            try:
                status, counts = db.vote_feedback(fid, client_ip, vote)
            except KeyError:
                return _error(f"feedback id={fid} 不存在", 404)
            return jsonify({
                "ok": True,
                "id": fid,
                "status": status,   # ok / already_same / changed
                "upvotes": counts["upvotes"],
                "downvotes": counts["downvotes"],
            })
        except ValueError as e:
            return _error(str(e))

    # ----------------------------------------------------------
    # 科普内容：缓存优先 -> LLM 流式生成 -> 回写缓存
    #   GET /api/culture/<region>
    #   - 命中缓存：一次性 JSON 返回 {"from_cache": true, "content": {...}}
    #   - 未命中  ：SSE 流式 token；done 事件里带 parsed；
    #               生成完毕后在后台线程回写缓存（避免阻塞 stream）
    # ----------------------------------------------------------
    @app.get("/api/culture/<path:region>")
    def _culture(region: str):
        region = (region or "").strip()
        if not region:
            return _error("region 不能为空")

        # 缓存命中：直接 JSON
        cached = db.get_culture_cache(region)
        if cached is not None:
            return jsonify({"from_cache": True, "content": cached})

        # 未命中：SSE 流式，生成完写回
        try:
            model_id = request.args.get("model_id")
            resolved, gen = ls.generate_culture_stream(region, model_id=model_id)
        except ls.LLMConfigError as e:
            return _error(str(e), 503)
        except ls.LLMAPIError as e:
            return _error(str(e), 502)

        collected: List[str] = []
        done_extra = {"model_id": resolved}

        def inner() -> Generator[str, None, None]:
            for tok in gen:
                collected.append(tok)
                yield f"event: token\ndata: {json.dumps(tok, ensure_ascii=False)}\n\n"
            full = "".join(collected)
            done_extra["raw"] = full
            parsed = None
            try:
                parsed = ls.parse_loose_json(full)
                done_extra["parsed"] = parsed
            except Exception as e:
                done_extra["parse_error"] = f"{type(e).__name__}: {e}"
            yield f"event: done\ndata: {json.dumps(done_extra, ensure_ascii=False)}\n\n"

            # 生成成功则异步回写缓存（线程）
            if parsed is not None:
                try:
                    t = threading.Thread(
                        target=db.set_culture_cache,
                        args=(region, parsed),
                        daemon=True,
                    )
                    t.start()
                except Exception:
                    pass

        return Response(
            stream_with_context(inner()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )
