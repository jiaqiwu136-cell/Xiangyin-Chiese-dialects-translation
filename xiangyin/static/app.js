/* ============================================================
 * 乡音 Xiangyin · 前端交互逻辑（方言 ↔ 英语）
 * ============================================================ */

(function () {
  "use strict";

  // ---------- DOM 引用 ----------
  const $ = (id) => document.getElementById(id);
  const dom = {
    modelSelect: $("modelSelect"),
    dirBtns:    document.querySelectorAll(".dir-btn"),
    srcInputD2E: $("srcInputD2E"),
    srcInputE2D: $("srcInputE2D"),
    targetDialect: $("targetDialect"),
    translateBtn: $("translateBtn"),
    cancelBtn:   $("cancelBtn"),
    statusText:  $("statusText"),
    originRow:   $("originRow"),
    originInput: $("originInput"),
    originMeta:  $("originMeta"),
    outMeta:     $("outMeta"),
    emptyOut:    $("emptyOut"),
    d2eResults:  $("d2eResults"),
    e2dResult:   $("e2dResult"),
    e2dTranslation: $("e2dTranslation"),
    e2dNotes:    $("e2dNotes"),
    cultureSection: $("cultureSection"),
    cultureCard: $("cultureCard"),
    cultureTag:  $("cultureTag"),
    boardList:   $("boardList"),
    disclaimerBox: $("disclaimerBox"),
    // feedback modal
    fbModal:     $("feedbackModal"),
    fbCloseBtn:  $("fbCloseBtn"),
    fbCancelBtn: $("fbCancelBtn"),
    fbForm:      $("feedbackForm"),
    fbSource:    $("fbSource"),
    fbTarget:    $("fbTarget"),
    fbOrigin:    $("fbOrigin"),
    fbSuggested: $("fbSuggested"),
    fbDirection: $("fbDirection"),
    fbModel:     $("fbModel"),
    fbTemp:      $("fbTemp"),
  };

  // ---------- 全局状态 ----------
  const STATE = {
    currentDir: "d2e",               // d2e / e2d
    currentModelId: "",              // 用户选择的模型
    originRegion: "",                // 用户确认的归属地
    lastResult: null,                // 上次翻译完整结果对象
    lastE2DResult: null,
    abortCtrl: null,                 // 用于取消请求
    boardItems: [],                  // 留言板数据
    translating: false,              // 全局翻译锁，防止 click 重入连环 self-abort
    // 投票去重：本地记录 feedback id -> 'up'/'down'（因为用IP去重，刷新后会恢复，但用户体验上我们存localStorage）
    localVotes: loadLocalVotes(),
  };

  function loadLocalVotes() {
    try {
      const raw = localStorage.getItem("xiangyin_votes");
      return raw ? JSON.parse(raw) : {};
    } catch (e) { return {}; }
  }
  function saveLocalVotes() {
    try { localStorage.setItem("xiangyin_votes", JSON.stringify(STATE.localVotes)); }
    catch (e) {}
  }

  // ============================================================
  // 通用：读选中的 model_id
  // ============================================================
  function currentModelId() {
    const v = dom.modelSelect.value;
    return v || ""; // "" → 后端走默认 provider
  }

  function setStatus(msg, type) {
    dom.statusText.textContent = msg || "";
    dom.statusText.className = "status-text" + (type ? " " + type : "");
  }

  // ============================================================
  // 通用：fetch SSE 解析器（POST + ReadableStream）
  //   onToken(token)  - 每个 token 回调
  //   onDone(doneObj) - SSE 事件 done 时回调
  //   返回 Promise<doneObj>
  // ============================================================
  function fetchSSE(url, body, { onToken, onDone } = {}) {
    if (STATE.abortCtrl) STATE.abortCtrl.abort();
    const ctrl = new AbortController();
    STATE.abortCtrl = ctrl;

    return fetch(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
      },
      body: JSON.stringify(body),
      signal: ctrl.signal,
    }).then(async (resp) => {
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}`;
        try {
          const j = await resp.json();
          if (j && j.error) msg = j.error;
        } catch (e) {}
        throw new Error(msg);
      }
      // 某些情况下后端直接返回 JSON（如科普缓存命中），不是 SSE
      const ctype = resp.headers.get("Content-Type") || "";
      if (!ctype.includes("text/event-stream")) {
        const json = await resp.json();
        if (onDone) onDone(json);
        return json;
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      let curEvent = "token";
      let lastDone = null;

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // 按空行拆 SSE
        while (true) {
          const sep = buffer.indexOf("\n\n");
          if (sep === -1) break;
          const eventStr = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);

          let dataStr = "";
          eventStr.split("\n").forEach((line) => {
            if (line.startsWith("event:")) {
              curEvent = line.slice("event:".length).trim();
            } else if (line.startsWith("data:")) {
              if (dataStr) dataStr += "\n";
              dataStr += line.slice("data:".length);
            }
          });
          if (!dataStr) continue;

          if (curEvent === "token") {
            let tok;
            try { tok = JSON.parse(dataStr); }
            catch (e) { tok = dataStr; }
            if (onToken) onToken(tok);
          } else if (curEvent === "done") {
            try {
              const obj = JSON.parse(dataStr);
              lastDone = obj;
              // done 事件触发意味着 SSE 数据结束。先解除 STATE 绑定，再调用 onDone。
              // 因为 onDone 里常常会调用 loadCulture / 另一个 fetchSSE，它们的 self-abort
              // 会误伤当前已经完成的请求，让 Chromium 回溯性标记 net::ERR_ABORTED。
              if (STATE.abortCtrl === ctrl) {
                STATE.abortCtrl = null;
              }
              if (onDone) onDone(obj);
            } catch (e) {
              console.warn("parse done event fail", e, dataStr);
            }
            curEvent = "token";
          }
        }
      }
      // 流已完整读完：立即解除 STATE.abortCtrl 绑定，避免后续任何清理（如 self-abort / 预览层）
      // 对已完成请求执行 abort() 会让 Chromium 回溯性标记为 net::ERR_ABORTED。
      if (STATE.abortCtrl === ctrl) {
        STATE.abortCtrl = null;
      }
      return lastDone;
    }).finally(() => {
      if (STATE.abortCtrl === ctrl) STATE.abortCtrl = null;
    });
  }

  function cancelStreaming() {
    if (STATE.abortCtrl) {
      STATE.abortCtrl.abort();
      STATE.abortCtrl = null;
    }
  }

  // ============================================================
  // 翻译方向切换
  // ============================================================
  function switchDir(dir) {
    STATE.currentDir = dir;
    document.querySelectorAll(".dir-btn").forEach((b) => {
      const active = b.dataset.dir === dir;
      b.classList.toggle("active", active);
      b.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".dir-panel").forEach((p) => {
      p.hidden = p.dataset.dirPanel !== dir;
    });
    // 清空输出区
    dom.emptyOut.hidden = true;
    dom.d2eResults.hidden = true;
    dom.e2dResult.hidden = true;
    dom.d2eResults.innerHTML = "";
    dom.e2dTranslation.innerHTML = "";
    dom.e2dNotes.textContent = "";
    dom.outMeta.textContent = "";
    dom.originRow.hidden = true;
    dom.originInput.value = "";
    dom.originMeta.textContent = "";
    dom.cultureSection.hidden = true;
    setStatus("");
  }

  dom.dirBtns.forEach((btn) => {
    btn.addEventListener("click", () => switchDir(btn.dataset.dir));
  });

  dom.cancelBtn.addEventListener("click", () => {
    cancelStreaming();
    setStatus("已停止", "ok");
    dom.cancelBtn.hidden = true;
    dom.translateBtn.disabled = false;
  });

  // ============================================================
  // 主翻译按钮
  // ============================================================
  dom.translateBtn.addEventListener("click", () => {
    if (STATE.currentDir === "d2e") doD2E();
    else doE2D();
  });

  // ============================================================
  // 方言→英语 流程
  //   step 1: 归属地推理 → 让用户确认
  //   用户点击「确认归属地」按钮再调 d2e 翻译
  //   （更顺滑：如果归属地推理完成并显示，originInput旁边出现
  //     一个「确认 & 翻译」按钮；用户可直接修改originInput再点它）
  // ============================================================
  function doD2E() {
    if (STATE.translating) return;
    STATE.translating = true;

    const text = dom.srcInputD2E.value.trim();
    if (!text) {
      STATE.translating = false;
      setStatus("请先输入方言文本", "err");
      return;
    }

    setStatus("正在推理方言归属地…");
    dom.translateBtn.disabled = true;
    dom.cancelBtn.hidden = false;

    // 重置输出
    dom.emptyOut.hidden = true;
    dom.d2eResults.hidden = true;
    dom.d2eResults.innerHTML = "";
    dom.e2dResult.hidden = true;
    dom.cultureSection.hidden = true;
    dom.outMeta.textContent = "";

    // ---- step 1：归属地推理 ----
    dom.originRow.hidden = false;
    dom.originInput.value = "";
    dom.originInput.readOnly = true;
    dom.originMeta.textContent = "";

    let originBuf = "";
    fetchSSE("/api/translate/infer-origin", {
      text, model_id: currentModelId(),
    }, {
      onToken: (tok) => {
        originBuf += tok;
        dom.originInput.value = originBuf;
      },
      onDone: (done) => {
        dom.originInput.readOnly = false;
        dom.cancelBtn.hidden = true;
        const parsed = done && done.parsed;
        if (parsed && typeof parsed === "object") {
          if (typeof parsed.origin === "string" && !dom.originInput.value.includes("{") ) {
            // 如果 token 流只输出纯字符串（未解析到整个对象的 token），用 parsed
            dom.originInput.value = parsed.origin;
          } else if (typeof parsed.origin === "string") {
            dom.originInput.value = parsed.origin;
          }
          // 展示 confidence / candidates / reasoning
          const metaParts = [];
          if (typeof parsed.confidence === "number") {
            const c = parsed.confidence;
            const cls = c >= 0.8 ? "conf-high" : (c >= 0.5 ? "conf-mid" : "conf-low");
            const txt = c >= 0.8 ? "高置信" : (c >= 0.5 ? "中等置信" : "低置信");
            metaParts.push(`置信度：<span class="${cls}">${txt} (${c.toFixed(2)})</span>`);
          }
          if (Array.isArray(parsed.candidates) && parsed.candidates.length) {
            metaParts.push(`候选：${parsed.candidates.slice(0,3).join("、")}`);
          }
          if (typeof parsed.reasoning === "string") {
            metaParts.push(`推理：${escapeHtml(parsed.reasoning)}`);
          }
          dom.originMeta.innerHTML = metaParts.join("｜");
        } else if (done && done.parse_error) {
          dom.originMeta.innerHTML = `<span class="conf-low">归属地JSON解析失败，您可手动修改上方文本</span>`;
        }

        STATE.originRegion = dom.originInput.value.trim();

        // 确认按钮：替换/追加一个确认翻译按钮到 originRow 下方
        ensureConfirmButton();

        dom.translateBtn.disabled = false;
        if (STATE.originRegion) {
          setStatus("归属地推理完成，请核对后点击「确认归属地并翻译」。");
        } else {
          setStatus("未能识别归属地，请手动填写后点击按钮。", "err");
        }
        STATE.translating = false;
      },
    }).catch((e) => {
      const isAbort = e.name === "AbortError";
      dom.originInput.readOnly = false;
      if (!isAbort) {
        dom.translateBtn.disabled = false;
        dom.cancelBtn.hidden = true;
        setStatus(`归属地推理失败：${e.message}`, "err");
      }
      STATE.translating = false;
      ensureConfirmButton();
    });
  }

  function ensureConfirmButton() {
    let btn = document.getElementById("originConfirmBtn");
    if (!btn) {
      btn = document.createElement("button");
      btn.type = "button";
      btn.id = "originConfirmBtn";
      btn.className = "btn-primary";
      btn.style.marginTop = "10px";
      btn.textContent = "✓ 确认归属地并翻译";
      dom.originRow.appendChild(btn);
      btn.addEventListener("click", doD2EStep2);
    }
    btn.disabled = false;
  }

  function doD2EStep2() {
    if (STATE.translating) return;
    STATE.translating = true;

    const origin = dom.originInput.value.trim();
    const text = dom.srcInputD2E.value.trim();
    if (!origin) { STATE.translating = false; setStatus("请先填写归属地", "err"); return; }
    if (!text)   { STATE.translating = false; setStatus("请先输入方言文本", "err"); return; }
    STATE.originRegion = origin;

    setStatus(`正在将 [${origin}] 方言译为英语（三版本）…`);
    dom.translateBtn.disabled = true;
    dom.cancelBtn.hidden = false;
    dom.d2eResults.hidden = false;
    dom.emptyOut.hidden = true;
    dom.d2eResults.innerHTML = "";

    // 先创建三个占位卡片（通过期望数量 3）
    const cardSpecs = [
      { key: 0, label: "Faithful Literal 忠实直译", temp: 0.3 },
      { key: 1, label: "Natural Fluent 自然通顺", temp: 0.7 },
      { key: 2, label: "Idiomatic Free 地道意译", temp: 1.2 },
    ];
    cardSpecs.forEach((spec) => {
      dom.d2eResults.appendChild(createD2ECard(spec, text, origin));
    });
    const cards = [...dom.d2eResults.querySelectorAll(".ver-card")];

    fetchSSE("/api/translate/d2e", {
      text, origin, model_id: currentModelId(),
    }, {
      onToken: () => {
        // d2e 是一次性返回完整 JSON，不适合逐词流式显示三卡。
        // 我们只在第一张卡显示流（完整 JSON token），让用户知道进度。
        const firstBody = cards[0] && cards[0].querySelector(".ver-body");
        if (firstBody) {
          firstBody.classList.add("streaming");
        }
      },
      onDone: (done) => {
        dom.cancelBtn.hidden = true;
        dom.translateBtn.disabled = false;
        const parsed = done && done.versions ? done : (done && done.parsed);
        if (done && done.model_id) {
          dom.outMeta.textContent = `模型：${done.model_id}`;
        }
        if (!parsed || !Array.isArray(parsed.versions)) {
          cards.forEach((c, i) => {
            const body = c.querySelector(".ver-body");
            body.classList.remove("streaming");
            if (i === 0) {
              body.innerHTML = `<span style="color:var(--accent)">解析失败：${escapeHtml((done && done.parse_error) || "结构不符合预期")}</span>`;
            } else {
              body.textContent = "—";
            }
          });
          setStatus("翻译返回结构异常", "err");
          return;
        }

        // 三版本对齐：按卡片顺序渲染
        STATE.lastResult = {
          sourceText: text, origin, modelId: done && done.model_id,
          versions: parsed.versions,
        };

        parsed.versions.forEach((v, idx) => {
          const card = cards[idx] || dom.d2eResults.appendChild(
            createD2ECard({key: idx, label: (v && v.label) || `版本${idx+1}`, temp: (v && v.temperature) || null }, text, origin)
          );
          fillD2ECard(card, v, idx, text, origin, done && done.model_id);
        });

        // 卡片数多于 3 就删掉空占位（极少）
        if (parsed.versions.length < cards.length) {
          cards.slice(parsed.versions.length).forEach((c) => c.remove());
        }

        setStatus(`翻译完成，共 ${parsed.versions.length} 个版本`, "ok");

        // 触发科普内容
        loadCulture(origin);
        STATE.translating = false;
      },
    }).catch((e) => {
      const isAbort = e.name === "AbortError";
      if (!isAbort) {
        dom.cancelBtn.hidden = true;
        dom.translateBtn.disabled = false;
        setStatus(`翻译失败：${e.message}`, "err");
      }
      STATE.translating = false;
      cards.forEach((c) => {
        const b = c.querySelector(".ver-body");
        b.classList.remove("streaming");
      });
    });
  }

  function createD2ECard(spec, text, origin) {
    const card = document.createElement("article");
    card.className = "ver-card";
    card.dataset.tempKey = spec.key;
    if (typeof spec.temp === "number") card.dataset.temp = spec.temp;
    card.innerHTML = `
      <div class="ver-head">
        <div class="ver-label">
          <span>${escapeHtml(spec.label || `版本 ${spec.key+1}`)}</span>
          ${typeof spec.temp === "number" ? `<span class="ver-temp">T=${spec.temp}</span>` : ""}
        </div>
        <div class="ver-actions">
          <button class="icon-btn fb-btn" type="button" title="反馈/修正">📝 反馈</button>
        </div>
      </div>
      <div class="ver-body streaming">等待结果…</div>
    `;
    // 绑定反馈按钮
    card.querySelector(".fb-btn").addEventListener("click", () => {
      openFeedback({
        direction: "dialect_to_english",
        sourceText: text,
        targetText: card.querySelector(".ver-body").textContent,
        originRegion: origin,
        temperature: card.dataset.temp ? Number(card.dataset.temp) : null,
      });
    });
    return card;
  }

  function fillD2ECard(card, version, idx, text, origin, modelId) {
    const label = (version && version.label) || `版本 ${idx+1}`;
    const temp  = (version && typeof version.temperature === "number") ? version.temperature : null;
    const trans = (version && typeof version.translation === "string") ? version.translation : "";
    const align = (version && Array.isArray(version.alignment)) ? version.alignment : [];

    // 更新头部
    const labelEl = card.querySelector(".ver-label");
    if (labelEl) {
      labelEl.innerHTML =
        `<span>${escapeHtml(label)}</span>` +
        (temp !== null ? ` <span class="ver-temp">T=${temp}</span>` : "");
    }
    if (temp !== null) card.dataset.temp = String(temp);

    // 译文体：根据 alignment 渲染逐词高亮 span
    const body = card.querySelector(".ver-body");
    body.classList.remove("streaming");
    renderD2EBody(body, trans, align, text);

    // 更新反馈按钮：带当前版本的 temperature/model_id
    card.querySelector(".fb-btn").onclick = () => {
      openFeedback({
        direction: "dialect_to_english",
        sourceText: text,
        targetText: trans,
        originRegion: origin,
        modelId: modelId || "",
        temperature: temp,
      });
    };
  }

  // ---------- 逐词对齐高亮：把译文按 tgt 范围切 span；同时在 data 属性
  //            保存 src 范围 id；悬停时联动所有同名的 span
  // ----------
  function renderD2EBody(bodyEl, translation, alignment, sourceText) {
    if (!translation) { bodyEl.textContent = ""; return; }
    if (!alignment || !alignment.length) { bodyEl.textContent = translation; return; }

    // 根据 alignment 的 tgt_start / tgt_end 构建译文片段
    const n = translation.length;
    const segs = []; // 数组每段 {text, aidx}
    // 以每个 tgt 字符为单位，找它落入哪个 align 条目
    // 若一个 align 条目 tgt 重叠：后者覆盖前者
    const charMap = new Array(n).fill(-1); // 每个字符属于哪个 align 条目 idx
    alignment.forEach((a, i) => {
      if (!a || typeof a.tgt_start !== "number" || typeof a.tgt_end !== "number") return;
      const s = Math.max(0, a.tgt_start);
      const e = Math.min(n, a.tgt_end);
      for (let k = s; k < e; k++) charMap[k] = i;
    });
    // 合并不变片段
    let runStart = 0;
    for (let i = 1; i <= n; i++) {
      if (i === n || charMap[i] !== charMap[runStart]) {
        segs.push({
          text: translation.slice(runStart, i),
          aidx: charMap[runStart],
        });
        runStart = i;
      }
    }
    bodyEl.innerHTML = "";
    segs.forEach((seg) => {
      if (seg.aidx === -1) {
        bodyEl.appendChild(document.createTextNode(seg.text));
      } else {
        const span = document.createElement("span");
        span.className = "align-tgt";
        span.dataset.alignIdx = String(seg.aidx);
        span.textContent = seg.text;
        span.addEventListener("mouseenter", () => setHighlight(String(seg.aidx), true));
        span.addEventListener("mouseleave", () => setHighlight(String(seg.aidx), false));
        bodyEl.appendChild(span);
      }
    });

    // 同步在原文文本上方追加一条"原文对齐行"，放在 bodyEl 的最前面
    // （不重复放原文，而是在卡片的 body 顶部插入）
    // 考虑到 UI 简洁，这里采用更轻量的做法：鼠标悬停时弹出 note 展示 source 范围对应文字
    // 但 PRD 要求"鼠标悬停在译文词语上时，高亮显示原文对应部分"。
    // 所以我们在 bodyEl 顶部再渲染一段 source 原文 align 串：
    const srcRow = document.createElement("div");
    srcRow.className = "source-align-row";
    srcRow.style.cssText = "font-size:12px;color:var(--ink-muted);border-bottom:1px dashed var(--line);padding-bottom:6px;margin-bottom:8px;line-height:1.9;";
    renderSourceAlign(srcRow, sourceText, alignment);
    // 只有当前卡片还没加过才加
    if (!bodyEl.dataset.hasSourceRow) {
      bodyEl.dataset.hasSourceRow = "1";
      bodyEl.parentNode.insertBefore(srcRow, bodyEl);
      // 把 note 以 tooltip 的形式挂在 align-tgt 上
      bodyEl.querySelectorAll(".align-tgt").forEach((el) => {
        const ai = Number(el.dataset.alignIdx);
        const a = alignment[ai];
        if (a) {
          const src = sourceText.slice(
            Math.max(0, a.src_start|0),
            Math.min(sourceText.length, a.src_end|0)
          );
          el.title = `原文：${src}${a.note ? "｜" + a.note : ""}`;
        }
      });
    }
  }

  function renderSourceAlign(container, sourceText, alignment) {
    if (!sourceText) { container.remove(); return; }
    container.appendChild(document.createTextNode("原文切片："));
    const n = sourceText.length;
    const charMap = new Array(n).fill(-1);
    alignment.forEach((a, i) => {
      if (!a || typeof a.src_start !== "number" || typeof a.src_end !== "number") return;
      const s = Math.max(0, a.src_start);
      const e = Math.min(n, a.src_end);
      for (let k = s; k < e; k++) charMap[k] = i;
    });
    let runStart = 0;
    const segs = [];
    for (let i = 1; i <= n; i++) {
      if (i === n || charMap[i] !== charMap[runStart]) {
        segs.push({ text: sourceText.slice(runStart, i), aidx: charMap[runStart] });
        runStart = i;
      }
    }
    segs.forEach((seg) => {
      if (seg.aidx === -1) {
        container.appendChild(document.createTextNode(seg.text));
      } else {
        const span = document.createElement("span");
        span.className = "align-src";
        span.dataset.alignIdx = String(seg.aidx);
        span.textContent = seg.text;
        span.addEventListener("mouseenter", () => setHighlight(String(seg.aidx), true));
        span.addEventListener("mouseleave", () => setHighlight(String(seg.aidx), false));
        const a = alignment[seg.aidx];
        if (a && a.note) span.title = a.note;
        container.appendChild(span);
      }
    });
  }

  function setHighlight(alignIdxStr, on) {
    document.querySelectorAll(`[data-align-idx="${alignIdxStr}"]`).forEach((el) => {
      el.classList.toggle("highlight", on);
    });
  }

  // ============================================================
  // 英语 → 方言
  // ============================================================
  function doE2D() {
    if (STATE.translating) return;
    STATE.translating = true;

    const text = dom.srcInputE2D.value.trim();
    const target = dom.targetDialect.value.trim();
    if (!text)   { STATE.translating = false; setStatus("请先输入英语文本", "err"); return; }
    if (!target) { STATE.translating = false; setStatus("请填写目标方言", "err"); return; }

    setStatus(`正在翻译为 [${target}]…`);
    dom.translateBtn.disabled = true;
    dom.cancelBtn.hidden = false;
    dom.emptyOut.hidden = true;
    dom.d2eResults.hidden = true;
    dom.d2eResults.innerHTML = "";
    dom.e2dResult.hidden = false;
    dom.e2dTranslation.innerHTML = '<span class="streaming">等待结果…</span>';
    dom.e2dNotes.textContent = "";
    dom.cultureSection.hidden = true;
    dom.outMeta.textContent = "";

    fetchSSE("/api/translate/e2d", {
      text, target_dialect: target, model_id: currentModelId(),
    }, {
      onToken: () => {
        // 同样：SSE 传 JSON token，只在最终显示；但为了视觉反馈，加个 streaming 光标动画即可
      },
      onDone: (done) => {
        dom.cancelBtn.hidden = true;
        dom.translateBtn.disabled = false;
        const parsed = done && done.translation ? done : (done && done.parsed);
        if (done && done.model_id) {
          dom.outMeta.textContent = `模型：${done.model_id}`;
        }
        if (!parsed || typeof parsed.translation !== "string") {
          dom.e2dTranslation.classList.remove("streaming");
          dom.e2dTranslation.innerHTML = `<span style="color:var(--accent)">解析失败：${escapeHtml((done && done.parse_error) || "结构不符合预期")}</span>`;
          setStatus("翻译返回结构异常", "err");
          return;
        }
        const trans = parsed.translation;
        const pron  = Array.isArray(parsed.pronunciation) ? parsed.pronunciation : [];
        const notes = typeof parsed.notes === "string" ? parsed.notes : "";
        STATE.lastE2DResult = {
          sourceText: text, targetDialect: target,
          translation: trans, pronunciation: pron,
          modelId: done && done.model_id,
        };
        renderE2D(trans, pron);
        dom.e2dNotes.textContent = notes;
        // 追加反馈按钮
        let actions = dom.e2dResult.querySelector(".e2d-actions");
        if (!actions) {
          actions = document.createElement("div");
          actions.className = "e2d-actions";
          dom.e2dResult.appendChild(actions);
        }
        actions.innerHTML = "";
        const fb = document.createElement("button");
        fb.className = "btn-primary";
        fb.type = "button";
        fb.textContent = "📝 反馈 / 提交您的修正版本";
        fb.addEventListener("click", () => {
          openFeedback({
            direction: "english_to_dialect",
            sourceText: text,
            targetText: trans,
            originRegion: target,
            modelId: done && done.model_id,
          });
        });
        actions.appendChild(fb);

        setStatus(`翻译为 [${target}] 完成`, "ok");
        loadCulture(target);
        STATE.translating = false;
      },
    }).catch((e) => {
      const isAbort = e.name === "AbortError";
      if (!isAbort) {
        dom.cancelBtn.hidden = true;
        dom.translateBtn.disabled = false;
        setStatus(`翻译失败：${e.message}`, "err");
      }
      STATE.translating = false;
    });
  }

  function renderE2D(translation, pronunciation) {
    const transEl = dom.e2dTranslation;
    transEl.classList.remove("streaming");
    transEl.innerHTML = "";
    const chars = [...translation]; // Unicode 码点拆分（大致满足汉字）
    if (!pronunciation || pronunciation.length !== chars.length) {
      // 长度不匹配：直接输出 ruby
      for (let i = 0; i < chars.length; i++) {
        const ruby = document.createElement("ruby");
        const rb = document.createElement("rb"); rb.textContent = chars[i];
        const rt = document.createElement("rt");
        const p = pronunciation && pronunciation[i];
        if (p && typeof p.pinyin === "string") rt.textContent = p.pinyin;
        else if (p && typeof p.char === "string" && p.char === chars[i]) rt.textContent = "";
        ruby.appendChild(rb);
        ruby.appendChild(rt);
        transEl.appendChild(ruby);
      }
      return;
    }
    // 严格长度匹配
    for (let i = 0; i < chars.length; i++) {
      const p = pronunciation[i];
      const ruby = document.createElement("ruby");
      const rb = document.createElement("rb");
      rb.textContent = chars[i];
      const rt = document.createElement("rt");
      rt.textContent = (p && typeof p.pinyin === "string") ? p.pinyin : "";
      ruby.appendChild(rb);
      ruby.appendChild(rt);
      transEl.appendChild(ruby);
    }
  }

  // ============================================================
  // 科普卡片
  // ============================================================
  function loadCulture(region) {
    if (!region) return;
    dom.cultureSection.hidden = false;
    dom.cultureCard.innerHTML = '<div class="culture-loading">正在加载方言科普内容…</div>';
    dom.cultureTag.textContent = "";
    dom.cultureTag.className = "cache-tag";

    // 科普接口可能缓存命中（直接返回 JSON）也可能走 SSE
    const qs = currentModelId() ? `?model_id=${encodeURIComponent(currentModelId())}` : "";
    const url = `/api/culture/${encodeURIComponent(region)}${qs}`;

    // 先走 fetch 判断 Content-Type
    if (STATE.abortCtrl) STATE.abortCtrl.abort();
    const ctrl = new AbortController(); STATE.abortCtrl = ctrl;

    fetch(url, { signal: ctrl.signal }).then(async (resp) => {
      if (!resp.ok) {
        let msg = `HTTP ${resp.status}`;
        try { const j = await resp.json(); if (j && j.error) msg = j.error; } catch (e) {}
        throw new Error(msg);
      }
      const ctype = resp.headers.get("Content-Type") || "";
      if (!ctype.includes("text/event-stream")) {
        const json = await resp.json();
        dom.cultureTag.textContent = "✓ 命中缓存";
        dom.cultureTag.classList.add("hit");
        // 缓存命中（非流式）：立即解除 STATE 绑定，避免后续 self-abort 误伤
        if (STATE.abortCtrl === ctrl) STATE.abortCtrl = null;
        renderCulture(json.content || json);
        return;
      }
      // SSE 流
      const reader = resp.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      let curEvent = "token";
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        while (true) {
          const sep = buffer.indexOf("\n\n");
          if (sep === -1) break;
          const eventStr = buffer.slice(0, sep);
          buffer = buffer.slice(sep + 2);
          let dataStr = "";
          eventStr.split("\n").forEach((line) => {
            if (line.startsWith("event:")) curEvent = line.slice("event:".length).trim();
            else if (line.startsWith("data:")) {
              if (dataStr) dataStr += "\n";
              dataStr += line.slice("data:".length);
            }
          });
          if (!dataStr) continue;
          if (curEvent === "token") {
            // 科普整体是 JSON，不需要逐 token 渲染
          } else if (curEvent === "done") {
            try {
              const obj = JSON.parse(dataStr);
              const content = obj.parsed || obj;
              dom.cultureTag.textContent = "✨ AI 生成";
              // done 事件：先解除 STATE 绑定再渲染，避免后续 self-abort 误伤
              if (STATE.abortCtrl === ctrl) STATE.abortCtrl = null;
              renderCulture(content);
            } catch (e) {
              dom.cultureCard.innerHTML = `<div class="culture-loading" style="color:var(--accent)">科普解析失败：${escapeHtml(e.message)}</div>`;
            }
            curEvent = "token";
          }
        }
      }
      // SSE 流读完：兜底再解除一次 STATE 绑定（若没走 done 分支）
      if (STATE.abortCtrl === ctrl) STATE.abortCtrl = null;
    }).catch((e) => {
      if (STATE.abortCtrl === ctrl) STATE.abortCtrl = null;
      if (e && e.name === "AbortError") return;
      dom.cultureCard.innerHTML = `<div class="culture-loading" style="color:var(--accent)">科普加载失败：${escapeHtml(e.message)}</div>`;
    });
  }

  function renderCulture(data) {
    if (!data || typeof data !== "object") {
      dom.cultureCard.innerHTML = '<div class="culture-loading">暂无科普内容</div>';
      return;
    }
    const card = dom.cultureCard;
    card.innerHTML = "";

    const title = document.createElement("h3");
    title.className = "culture-title";
    title.textContent = typeof data.title === "string" ? data.title : "乡土文化";
    card.appendChild(title);

    if (typeof data.summary === "string") {
      const s = document.createElement("p");
      s.className = "culture-summary";
      s.textContent = data.summary;
      card.appendChild(s);
    }

    if (Array.isArray(data.sections)) {
      const wrap = document.createElement("div");
      wrap.className = "culture-sections";
      data.sections.forEach((sec) => {
        if (!sec || typeof sec !== "object") return;
        const box = document.createElement("div");
        box.className = "culture-sec";
        if (typeof sec.heading === "string") {
          const h = document.createElement("h3");
          h.textContent = sec.heading;
          box.appendChild(h);
        }
        if (typeof sec.body === "string") {
          const p = document.createElement("p");
          p.textContent = sec.body;
          box.appendChild(p);
        }
        if (Array.isArray(sec.examples) && sec.examples.length) {
          const row = document.createElement("div");
          row.className = "culture-examples";
          sec.examples.forEach((e) => {
            if (!e) return;
            const d = document.createElement("div");
            d.className = "ex";
            d.innerHTML =
              `<span class="src">${escapeHtml(String(e.src || ""))}</span>` +
              `<span class="tgt">${escapeHtml(String(e.tgt || ""))}</span>`;
            row.appendChild(d);
          });
          box.appendChild(row);
        }
        wrap.appendChild(box);
      });
      card.appendChild(wrap);
    }

    if (Array.isArray(data.tags) && data.tags.length) {
      const t = document.createElement("div");
      t.className = "culture-tags";
      data.tags.forEach((name) => {
        if (!name) return;
        const span = document.createElement("span");
        span.className = "tag";
        span.textContent = String(name);
        t.appendChild(span);
      });
      card.appendChild(t);
    }

    if (Array.isArray(data.related_dialects) && data.related_dialects.length) {
      const r = document.createElement("div");
      r.className = "culture-related";
      r.innerHTML = "<b>相近方言：</b>" + data.related_dialects
        .filter(Boolean).map((x) => escapeHtml(String(x))).join(" / ");
      card.appendChild(r);
    }
  }

  // ============================================================
  // 反馈 Modal
  // ============================================================
  function openFeedback(opts) {
    dom.fbDirection.value = opts.direction || "";
    dom.fbSource.value = opts.sourceText || "";
    dom.fbTarget.value = opts.targetText || "";
    dom.fbOrigin.value = opts.originRegion || "";
    dom.fbModel.value = opts.modelId || "";
    dom.fbTemp.value = opts.temperature !== null && opts.temperature !== undefined
      ? String(opts.temperature) : "";
    dom.fbSuggested.value = "";
    dom.fbModal.hidden = false;
    setTimeout(() => dom.fbSuggested.focus(), 50);
  }
  function closeFb() { dom.fbModal.hidden = true; }
  dom.fbCloseBtn.addEventListener("click", closeFb);
  dom.fbCancelBtn.addEventListener("click", closeFb);
  dom.fbModal.addEventListener("click", (e) => {
    if (e.target === dom.fbModal) closeFb();
  });

  dom.fbForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const suggested = dom.fbSuggested.value.trim();
    if (!suggested) {
      alert("请填写您认为正确的版本");
      return;
    }
    const body = {
      direction: dom.fbDirection.value,
      source_text: dom.fbSource.value,
      target_text: dom.fbTarget.value,
      origin_region: dom.fbOrigin.value,
      suggested_text: suggested,
      model_id: dom.fbModel.value || null,
      temperature: dom.fbTemp.value ? Number(dom.fbTemp.value) : null,
    };
    fetch("/api/feedbacks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then((r) => r.json()).then((json) => {
      if (!json || !json.ok) {
        alert(`提交失败：${(json && json.error) || "未知错误"}`);
        return;
      }
      closeFb();
      setStatus("反馈已提交，感谢您的贡献！", "ok");
      // 立即插入到留言板顶部
      if (json.item) {
        prependBoardItem(json.item);
      } else {
        loadBoard();
      }
    }).catch((e) => {
      alert(`提交失败：${e.message}`);
    });
  });

  // ============================================================
  // 留言板
  // ============================================================
  function loadBoard() {
    fetch("/api/feedbacks?limit=50").then((r) => r.json()).then((json) => {
      const items = Array.isArray(json.items) ? json.items : [];
      STATE.boardItems = items;
      renderBoard(items);
    }).catch((e) => {
      dom.boardList.innerHTML = `<div class="culture-loading" style="color:var(--accent)">加载失败：${escapeHtml(e.message)}</div>`;
    });
  }
  function prependBoardItem(item) {
    STATE.boardItems.unshift(item);
    renderBoard(STATE.boardItems);
  }
  function renderBoard(items) {
    dom.boardList.innerHTML = "";
    if (!items.length) {
      dom.boardList.innerHTML =
        '<div class="culture-loading">暂无反馈。欢迎成为第一位社区贡献者！</div>';
      return;
    }
    const frag = document.createDocumentFragment();
    items.forEach((it) => frag.appendChild(renderBoardItem(it)));
    dom.boardList.appendChild(frag);
  }
  function renderBoardItem(it) {
    const row = document.createElement("article");
    row.className = "board-item";
    row.dataset.id = String(it.id);

    const head = document.createElement("div");
    head.className = "board-head";
    const dirSpan = document.createElement("span");
    dirSpan.className = "board-direction " +
      (it.direction === "dialect_to_english" ? "d2e" : "e2d");
    dirSpan.textContent = it.direction === "dialect_to_english" ? "方言→英语" : "英语→方言";
    head.appendChild(dirSpan);
    if (it.origin_region) {
      const reg = document.createElement("span");
      reg.className = "board-region";
      reg.textContent = `｜${escapeHtml(it.origin_region)}`;
      head.appendChild(reg);
    }
    const meta = document.createElement("span");
    meta.className = "board-meta";
    const loc = it.submitter_location || "来自 未知地区 的用户";
    meta.textContent = `${escapeHtml(loc)} · ${escapeHtml(String(it.created_at || ""))}`;
    head.appendChild(meta);

    const text = document.createElement("div");
    text.className = "board-text";
    text.innerHTML =
      `<span class="row"><span class="label">原文：</span>${escapeHtml(it.source_text || "")}</span>` +
      (it.target_text ? `<span class="row"><span class="label">系统译文：</span>${escapeHtml(it.target_text)}</span>` : "") +
      `<span class="row"><span class="label">用户修正：</span><span class="suggested">${escapeHtml(it.suggested_text || "")}</span></span>`;

    const foot = document.createElement("div");
    foot.className = "board-foot";
    // 投票组
    const vg = document.createElement("div");
    vg.className = "vote-group";
    const upBtn = document.createElement("button");
    upBtn.className = "vote-btn up";
    upBtn.type = "button";
    upBtn.innerHTML = `👍 <span class="vote-count">${it.upvotes|0}</span>`;
    const downBtn = document.createElement("button");
    downBtn.className = "vote-btn down";
    downBtn.type = "button";
    downBtn.innerHTML = `👎 <span class="vote-count">${it.downvotes|0}</span>`;

    const myVote = STATE.localVotes[String(it.id)];
    if (myVote === "up") upBtn.classList.add("active");
    else if (myVote === "down") downBtn.classList.add("active");

    upBtn.addEventListener("click", () => voteItem(it.id, "up", upBtn, downBtn));
    downBtn.addEventListener("click", () => voteItem(it.id, "down", upBtn, downBtn));

    vg.appendChild(upBtn); vg.appendChild(downBtn);

    const right = document.createElement("span");
    right.style.cssText = "font-size:12px;color:var(--ink-muted);";
    right.textContent = it.model_id ? `模型：${escapeHtml(it.model_id)}${it.temperature !== null && it.temperature !== undefined ? ` · T=${it.temperature}` : ""}` : "";

    foot.appendChild(vg); foot.appendChild(right);

    row.appendChild(head);
    row.appendChild(text);
    row.appendChild(foot);
    return row;
  }

  function voteItem(fid, vote, upBtn, downBtn) {
    fetch(`/api/feedbacks/${fid}/vote`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ vote }),
    }).then((r) => r.json()).then((json) => {
      if (!json || !json.ok) {
        alert(`投票失败：${(json && json.error) || "未知错误"}`);
        return;
      }
      upBtn.querySelector(".vote-count").textContent = String(json.upvotes);
      downBtn.querySelector(".vote-count").textContent = String(json.downvotes);
      upBtn.classList.remove("active"); downBtn.classList.remove("active");
      // status ok=新投票；changed=切换；already_same=重复（保持active）
      if (json.status === "ok" || json.status === "changed") {
        STATE.localVotes[String(fid)] = vote;
      }
      if (vote === "up" && (json.status === "ok" || json.status === "changed" || json.status === "already_same")) {
        upBtn.classList.add("active");
      }
      if (vote === "down" && (json.status === "ok" || json.status === "changed" || json.status === "already_same")) {
        downBtn.classList.add("active");
      }
      saveLocalVotes();
      // 更新 STATE.boardItems 里的票数
      const it = STATE.boardItems.find((x) => String(x.id) === String(fid));
      if (it) { it.upvotes = json.upvotes; it.downvotes = json.downvotes; }
    }).catch((e) => alert(`投票失败：${e.message}`));
  }

  // ============================================================
  // 工具
  // ============================================================
  function escapeHtml(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // ============================================================
  // 初始化：拉模型池、免责声明、留言板
  // ============================================================
  async function initConfig() {
    try {
      const [models, disc] = await Promise.all([
        fetch("/api/models").then((r) => r.json()),
        fetch("/api/disclaimer").then((r) => r.json()),
      ]);
      if (models && Array.isArray(models.pool) && models.pool.length) {
        dom.modelSelect.innerHTML = "";
        models.pool.forEach((m) => {
          const opt = document.createElement("option");
          opt.value = m.id;
          opt.textContent = m.label + (m.is_default ? "  (默认)" : "");
          if (m.is_default) opt.selected = true;
          dom.modelSelect.appendChild(opt);
        });
      } else {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "（未配置模型，使用系统默认）";
        dom.modelSelect.innerHTML = "";
        dom.modelSelect.appendChild(opt);
      }
      STATE.currentModelId = dom.modelSelect.value;
      dom.modelSelect.addEventListener("change", () => {
        STATE.currentModelId = dom.modelSelect.value;
      });
      if (disc && typeof disc.text === "string") {
        dom.disclaimerBox.textContent = disc.text;
      }
    } catch (e) {
      dom.disclaimerBox.textContent = `免责声明加载失败：${e.message}`;
    }
  }

  // 启动
  window.addEventListener("DOMContentLoaded", async () => {
    await initConfig();
    loadBoard();
    switchDir("d2e");
  });
})();
