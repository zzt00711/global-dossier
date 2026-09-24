/* 流水线：同族 → 挑选 → 下载 → PDF.js 文本层 → 正则提取 → LLM 流式报告 */
"use strict";

const PIPE = (() => {
  const DEFAULTS = {
    offices: ["US", "EP", "JP", "KR"],
    maxDocs: 8,
    maxOcrPages: 40,
    docTextLen: 25666,
    textBudget: 256000,
    maxTokens: 32768,
    progressStep: 400,
    apiBase: "https://note3-prev-api.askdiandian.com/v1",
    model: "dots3-note-prev",
  };

  // ── 内置 key（XOR+base64 分段混淆，运行时组装；也可用"设置"里自填 key 覆盖）──
  function embeddedKey() {
    const S = ["OHYAIMFEEFG", "zC2yg2my2mT", "EIwo008Ag0A", "Fzu1jpLgW3="];
    let b = "";
    for (let i = 0; i < 11; i++) for (const s of S) if (i < s.length) b += s[i];
    const bin = atob(b);
    let k = "";
    for (const c of bin) k += String.fromCharCode(c.charCodeAt(0) ^ 0x5A);
    return k;
  }
  function apiKey() { return (localStorage.getItem("gd_llm_key") || "").trim() || embeddedKey(); }
  function apiBase() { return (localStorage.getItem("gd_llm_base") || "").trim() || DEFAULTS.apiBase; }
  function modelName() { return (localStorage.getItem("gd_llm_model") || "").trim() || DEFAULTS.model; }

  // ── PDF.js 文本层抽取 + tesseract.js 全量 OCR（Global Dossier 文书几乎都是扫描图像）──
  async function openPdf(buf) {
    return await pdfjsLib.getDocument({ data: new Uint8Array(buf), isEvalSupported: false }).promise;
  }
  async function layerText(pdf, maxPages) {
    const lines = [];
    const n = Math.min(pdf.numPages, maxPages);
    for (let p = 1; p <= n; p++) {
      try {
        const tc = await (await pdf.getPage(p)).getTextContent();
        let line = "";
        for (const it of tc.items) {
          if (typeof it.str !== "string") continue;
          line += it.str + (it.hasEOL ? "\n" : " ");
          if (line.includes("\n")) { lines.push(line.replace(/\n+$/, "")); line = ""; }
        }
        if (line.trim()) lines.push(line.trim());
        if (lines.join("\n").length > 2000000) break;
      } catch (e) { lines.push("[第" + p + "页解析失败]"); }
    }
    return lines.join("\n");
  }
  // OCR worker 缓存（按语言组合复用，避免重复加载 wasm/traineddata）
  const ocrWorkers = {};
  async function getOcrWorker(langs, log) {
    if (ocrWorkers[langs]) return ocrWorkers[langs];
    log(`  加载 OCR 引擎(${langs})，首次需下载语言包 ...`);
    try {
      const w = await Tesseract.createWorker(langs, 1, {
        workerPath: "./vendor/tesseract/worker.min.js",
        corePath: "./vendor/tesseract/",
        langPath: "./vendor/tesseract/",
        workerBlobURL: false,   // 直连同源脚本，避开 blob worker 的 importScripts 跨源限制
        gzip: true,
        logger: () => {},
      });
      ocrWorkers[langs] = w;
      return w;
    } catch (e) {
      log("  OCR 引擎加载失败: " + String(e && e.message || e).slice(0, 160));
      throw e;
    }
  }
  const OFFICE_LANGS = { US: "eng", EP: "eng", JP: "eng+jpn", KR: "eng+kor", WO: "eng" };
  const isPctNum = s => /\//.test(s || "");
  async function ocrPageTexts(pdf, langs, maxPages, log, cancel, onPages) {
    const worker = await getOcrWorker(langs, log);
    const n = Math.min(pdf.numPages, maxPages);
    const canvas = document.createElement("canvas");
    const parts = [];
    for (let p = 1; p <= n; p++) {
      if (isCancelled(cancel)) throw new Error("__CANCELLED__");
      const page = await pdf.getPage(p);
      const vp = page.getViewport({ scale: 2.6 });   // ≈187 dpi，OCR 性价比甜点
      canvas.width = Math.ceil(vp.width); canvas.height = Math.ceil(vp.height);
      const ctx = canvas.getContext("2d", { willReadFrequently: true });
      await page.render({ canvasContext: ctx, viewport: vp }).promise;
      const { data: { text } } = await worker.recognize(canvas);
      parts.push(text);
      onPages && onPages(p, n);
    }
    return parts.join("\n");
  }
  async function pdfToText(buf, opts, log, cancel, office) {
    const pdf = await openPdf(buf);
    let text = "";
    try { text = await layerText(pdf, opts.maxOcrPages || 40); } catch (e) { text = ""; }
    if (text.replace(/\s+/g, "").length >= 50) {          // 数字版：有文本层直接用
      try { await pdf.destroy(); } catch (e) {}
      return text;
    }
    if (typeof Tesseract === "undefined") { try { await pdf.destroy(); } catch (e) {} return "[扫描版-OCR引擎未加载]"; }
    try {
      const langs = OFFICE_LANGS[(office || "").toUpperCase()] || "eng";
      const t0 = Date.now();
      const text2 = await ocrPageTexts(pdf, langs, opts.maxOcrPages || 40, log, cancel,
        (p, n) => { if (p === 1 || p % 5 === 0 || p === n) log(`    OCR ${p}/${n} 页 (${((Date.now() - t0) / 1000).toFixed(0)}s) ...`); });
      try { await pdf.destroy(); } catch (e) {}
      return text2;
    } catch (e) {
      try { await pdf.destroy(); } catch (e2) {}
      if (e.message === "__CANCELLED__") throw e;
      return "[扫描版-OCR失败:" + String(e.message).slice(0, 40) + "]";
    }
  }

  const isCancelled = c => c && c.stop;
  function checkCancel(c) { if (isCancelled(c)) throw new Error("__CANCELLED__"); }

  // ── 单局处理（process_member 移植）──
  async function processMember(member, opts, log, cancel, excludeAll) {
    const country = (member.countryCode || "").toUpperCase(), appNum = member.appNum, kind = member.kindCode;
    const isPct = isPctNum(appNum);   // PCT 国际申请号（如 PCT/US22/45176），斜杠会破坏 URL 路径，需换写法
    checkCancel(cancel);
    log(`\n===== ${isPct ? "PCT(国际局)" : country} ${appNum} (kind=${kind}) =====`);
    let dl, dlCountry = country, dlBase = appNum;
    try {
      if (isPct) {
        // 优先用 WO 公布号查国际局清单（ISR 国际检索报告 / WOSA 书面意见 / IPRP1 等实审相关文书），
        // 拿不到再退回“去斜杠”申请号查受理局清单（RO/101、spec、claims 等受理文书）
        let got = null;
        const pub = (member.pubList || []).find(p => (p.pubCountry || "").toUpperCase() === "WO" && p.pubNum);
        if (pub) {
          const woNum = String(pub.pubNum).replace(/^WO/i, "");
          try { got = await GD.doclist("WO", woNum, "A"); dlCountry = "WO"; dlBase = woNum; }
          catch (e) { log(`  WO 公布号清单不可用(${e.message})，尝试受理局清单 ...`); }
        }
        if (!got) {
          const stripped = appNum.replace(/\//g, "");
          got = await GD.doclist(country, stripped, kind); dlBase = stripped;
        }
        dl = got;
      } else {
        dl = await GD.doclist(country, appNum, kind);
      }
    }
    catch (e) { log(`  文书列表失败: ${e.message}`); return { office: isPct ? "PCT/IB" : country, app_num: appNum, error: String(e.message).slice(0, 120), documents: [] }; }
    const officeLabel = dlCountry === "WO" || isPct ? "PCT/IB" : country;
    let docs = dl.docs || [];
    const docNumber = dl.docNumber;
    // PCT 国际阶段未进实审：国际局文书仅保留审查员关注的三份（ISR 国际检索报告 / WOSA 书面意见 / IPRP1 专利性国际报告），减少无谓 OCR 耗时
    if (isPct && dlCountry === "WO") {
      const KEEP = new Set(["ISR", "WOSA", "IPRP1"]);
      const before = docs.length;
      docs = docs.filter(d => KEEP.has((d.docCode || "").toUpperCase()));
      log(`  国际局文书 ${before} 份, 仅保留 ISR/WOSA/IPRP1 → ${docs.length} 份`);
    }
    // 排除全文公报类文书：专利全文文本（如 EP Text intended for grant、各局公开公报）动辄几十上百页，
    // OCR 耗时极长且不含审查过程信息；权利要求修改内容看 Amended claims 等提交文书即可
    const FULLTEXT = /text intended for grant|version for approval|clean copy|granted patent|patent specification|publication of a granted|issued (patent|document)|patent (grant )?publication|published (patent|application|invention)|pamphlet|公開公報|特許公報|公表|공개공보|등록공보/i;
    const dropped = [];
    docs = docs.filter(d => {
      const name = d.docDesc || "", pages = d.numberOfPages || 1;
      if (FULLTEXT.test(name)) { dropped.push(`${name}(${pages}p)`); return false; }
      if (pages > 60) { dropped.push(`${name}(${pages}p,超长)`); return false; }
      return true;
    });
    if (dropped.length) log(`  已排除全文/超长文书 ${dropped.length} 份: ${dropped.slice(0, 3).join("; ")}${dropped.length > 3 ? " ..." : ""}`);
    const picked = GD.pickExamDocs(docs, opts.maxDocs);
    log(`  文书共 ${docs.length} 份, 挑选实审相关 ${picked.length} 份`);
    const documents = [];
    for (const d of picked) {
      checkCancel(cancel);
      const name = d.docDesc, did = d.docId, pages = d.numberOfPages || 1;
      const isRefTail = /search\s+report|registered\s+search|international\s+(search|preliminary)|search\s+strategy|search\s+information|list\s+of\s+references|\b892\b|引用文献|인용문헌|검색|調査報告/.test(name.toLowerCase());
      const refLen = opts.docTextLen * (isRefTail ? 2 : 1);
      log(`  [${dlCountry}] ${name} (${pages}p) 下载 ...`);
      // 候选号：PCT 用清单返回的 docNumber(如 2023055894.W)/基础号；US 用 appNum；其他局依次 appNum / 去后缀 docNumber / 完整 docNumber
      let cands;
      if (isPct) cands = [docNumber, dlBase];
      else {
        cands = [appNum];
        if (country !== "US") cands.push((docNumber || "").split(".").slice(0, -1).join("."), docNumber || "");
        if (docNumber) cands.push(docNumber);
      }
      const seen = new Set(); const tryList = [];
      for (const c of cands) if (c && !seen.has(c)) { seen.add(c); tryList.push(c); }
      let pdf = null, used = "";
      for (const num of tryList) {
        const r = await GD.getPDF(dlCountry, num, did, pages, 1);
        if (r.ok) { pdf = r.buf; used = num; break; }
      }
      if (!pdf) {
        log("    下载失败");
        documents.push({ date: d.legalDateStr || "", name, code: d.docCode || "",
          type: GDX.detectDocType(name), pages, text: "", error: "全部候选下载失败" });
        continue;
      }
      log(`    下载完成 ${(pdf.byteLength / 1024) | 0}KB, 抽取文本 ...`);
      let text = "";
      try { text = await pdfToText(pdf, opts, log, cancel, dlCountry); } catch (e) { if (e.message === "__CANCELLED__") throw e; text = ""; }
      if (text.replace(/\s+/g, "").length < 50) {
        text = text.trim() || "[扫描版-未获取到文本]";
      } else {
        text = text.replace(/[ \t]+/g, " ");
        text = text.replace(/[ \t]*\n[ \t]*/g, "\n").trim();
        if (text.length > refLen) text = isRefTail ? text.slice(-refLen) : text.slice(0, refLen);
      }
      const isSearchLog = /search\s+(strategy|results?)/.test(name.toLowerCase());
      documents.push({
        date: d.legalDateStr || "", name, code: d.docCode || "",
        type: GDX.detectDocType(name), pages,
        citations: isSearchLog ? [] : GDX.extractCitations(text, 40, [appNum, ...(member.pubNum ? [member.pubNum] : []), ...excludeAll]),
        claims: GDX.extractClaims(text).slice(0, 10),
        conclusion: GDX.detectConclusion(text, name),
        text,
      });
      log(`    文本 ${text.length} 字, 引用 ${(documents[documents.length - 1].citations || []).length} 条, 结论: ${documents[documents.length - 1].conclusion}`);
    }
    const DEFINITIVE = new Set(["授权", "驳回", "视为撤回或放弃"]);
    let conclusion = "审查中";
    const dated = documents.filter(x => x.text && DEFINITIVE.has(x.conclusion));
    if (dated.length) {
      const dk = s => { s = (s || "").trim(); let m = s.match(/(\d{2})\/(\d{2})\/(\d{4})/); if (m) return [+m[3], +m[1], +m[2]]; m = s.match(/(\d{4})-(\d{2})-(\d{2})/); if (m) return [+m[1], +m[2], +m[3]]; return [9999, 12, 31]; };
      dated.sort((a, b) => { const ka = dk(a.date), kb = dk(b.date); return ka[0] - kb[0] || ka[1] - kb[1] || ka[2] - kb[2]; });
      conclusion = dated[dated.length - 1].conclusion;
    }
    return { office: officeLabel, app_num: appNum, kind_code: kind, title: member.title || "", conclusion, documents };
  }

  // ── 主流程 ──
  async function runPipeline(rawQuery, opts, log, cancel) {
    const t0 = Date.now();
    checkCancel(cancel);
    let q = (rawQuery || "").trim().toUpperCase();
    if (!/^(?:CN)?\d{6,12}[A-Z]?\d*$/.test(q)) throw new Error("请输入有效 CN 公开号，如 CN118076910 或 CN117460982A");
    const display = q.startsWith("CN") ? q : "CN" + q;
    if (q.startsWith("CN")) q = q.slice(2);
    q = q.replace(/[A-Z]\d*$/, "");   // 去 kind 后缀
    log(`[1/4] 查询同族: ${display}`);
    const members = await GD.family(q);
    checkCancel(cancel);
    const offices = new Set(opts.offices.map(s => s.toUpperCase()));
    const targets = members.filter(m => offices.has((m.countryCode || "").toUpperCase()));
    log(`同族 ${members.length} 个, 关注国外局 ${targets.length} 个: ` +
        targets.map(m => `${m.countryCode} ${m.appNum}`).join(", "));
    if (!targets.length) throw new Error("未找到国外局同族成员，请确认公开号或调整关注局");
    const excludeAll = [];
    for (const m of members) { if (m.appNum) excludeAll.push(m.appNum); if (m.pubNum) excludeAll.push(m.pubNum); }
    log("[2/4] 逐个国外局: 文书列表 → 下载 → 文本抽取 → 结构化提取");
    const processed = [];
    for (let i = 0; i < targets.length; i++) {
      checkCancel(cancel);
      log(`\n[${i + 1}/${targets.length}] ${targets[i].countryCode} ${targets[i].appNum}`);
      processed.push(await processMember(targets[i], opts, log, cancel, excludeAll));
    }
    log(`\n[3/4] 汇总数据 (用时 ${((Date.now() - t0) / 1000).toFixed(0)}s)`);
    const now = new Date(), p2 = n => String(n).padStart(2, "0");
    return {
      query: display,
      fetched_at: `${now.getFullYear()}-${p2(now.getMonth() + 1)}-${p2(now.getDate())} ${p2(now.getHours())}:${p2(now.getMinutes())}:${p2(now.getSeconds())}`,
      family_members: members,
      members: processed,
    };
  }

  // ── LLM 报告（llm_generate_report 移植，流式）──
  function budgetCopy(data) {
    const d = JSON.parse(JSON.stringify(data));
    let used = 0;
    for (const m of d.members) for (const doc of m.documents || []) {
      const txt = (doc.text || "").trim();
      if (!txt || txt.startsWith("[") || txt.length < 50) continue;
      const remain = DEFAULTS.textBudget - used;
      if (remain < 3000) { doc.text = ""; doc.text_note = "超出文本预算未收录，仅提供结构化预提取"; used = DEFAULTS.textBudget; }
      else if (used + txt.length > DEFAULTS.textBudget) { doc.text = txt.slice(0, remain) + " …[截断]"; used = DEFAULTS.textBudget; }
      else used += txt.length;
    }
    return d;
  }

  const SYSTEM_PROMPT =
    "你是一名资深的中华人民共和国专利审查员，同时精通美国(USPTO)、欧洲(EPO)、" +
    "日本(JPO)、韩国(KIPO)的实审制度、法条与审查实践。" +
    "请基于用户提供的 Global Dossier 国外局实审案卷数据，撰写一份" +
    "《国外局实审过程分析报告》，帮助中国审查员快速掌握该同族在国外的审查脉络。" +
    "核心要求：" +
    "1) 绝不编造。只引用数据中真实出现的文书名称、日期、法条、对比文件与权利要求；" +
    "缺失信息明确标注'数据未提供'。" +
    "2) 对比文件必须注明编号与类别(X/Y/A，参照各局惯例：US 的 PTO-892、EP 检索报告、" +
    "JP 引用文献、KR 인용문헌)，并说明其被引用的理由(最接近现有技术/结合启示等)。" +
    "3) 法条按各局规范表述：US 35 U.S.C. §102/§103/§112、EPC Art. 54/56/84、" +
    "特許法第29条(新規性/進歩性)、특허법 제29조。" +
    "4) 逐份通知书梳理：引用法条 → 引用对比文件 → 审查意见要点；" +
    "随后整理申请人答复与权利要求修改；最后给出最终结论(授权/驳回/视为撤回等)，" +
    "若授权则摘录授权的权利要求——**只需摘录独立权利要求**(不引用其他权利要求" +
    "的权项，如 US 的 'What is claimed is' 中的第 1 条及不依赖前项的权项)。" +
    "5) 输出 Markdown，表格与分节并用，可直接作为工作参考。";

  function userPrompt(data) {
    return "以下是 Global Dossier 获取的国外局实审数据(JSON)。每个成员的 documents 按日期升序，" +
      "text 为通知书/答复/检索文书文本(截取片段)，citations/claims/conclusion 为正则预提取结果。" +
      "报告落款日期使用 fetched_at。\n\n" +
      JSON.stringify(data, null, 1) + "\n\n" +
      "请撰写中文 Markdown 报告，结构如下：\n" +
      `# 国外局实审过程分析报告（${data.query || ""}）\n\n` +
      "## 一、跨局对比与中国审查员参考\n" +
      "(各国驳回理由与对比文件使用方式的异同、对中国实审的参考要点；可先给出结论性概括)\n\n" +
      "## 二、历次通知书详解（引用法条/对比文件/审查意见）\n" +
      "(逐局逐份：引用法条、引用对比文件[编号/类别X·Y·A/专利号/被引用理由]、审查意见要点)\n\n" +
      "## 三、申请人答复与权利要求修改\n" +
      "(历次答复要点、修改的权利要求、争辩理由；数据未提供时如实说明)\n\n" +
      "## 四、同族与实审范围总览\n" +
      "(同族成员、各局申请号、关注范围说明、各局最终结论汇总表)\n\n" +
      "## 五、各局实审时间线\n" +
      "(每局按时间列出历次通知书/答复/检索文书，含日期、类型、状态标签)\n\n" +
      "## 六、最终审查结论\n" +
      "(每局最终状态：授权/驳回/视为撤回等；若授权，摘录授权的独立权利要求文本)\n";
  }

  async function callLLM(data, onProgress, cancel) {
    checkCancel(cancel);
    const budgeted = budgetCopy(data);
    const body = {
      model: modelName(),
      messages: [{ role: "system", content: SYSTEM_PROMPT },
                 { role: "user", content: userPrompt(budgeted) }],
      max_tokens: DEFAULTS.maxTokens,
      stream: true,
      chat_template_kwargs: { enable_thinking: false },  // 关键：thinking 模式 content 为 null
    };
    onProgress(`  模型: ${modelName()}\n  接口: ${apiBase()}\n  [4/4] 大模型生成报告(流式) ...`);
    const r = await fetch(apiBase().replace(/\/$/, "") + "/chat/completions", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + apiKey() },
      body: JSON.stringify(body),
      signal: cancel.signal,
    });
    if (!r.ok) throw new Error("LLM HTTP " + r.status + ": " + (await r.text()).slice(0, 200));
    const reader = r.body.getReader(), dec = new TextDecoder();
    let bufTxt = "", parts = [], done = 0, lastReport = 0;
    while (true) {
      checkCancel(cancel);
      const { value, done: fin } = await reader.read();
      if (fin) break;
      bufTxt += dec.decode(value, { stream: true });
      let nl;
      while ((nl = bufTxt.indexOf("\n")) >= 0) {
        const line = bufTxt.slice(0, nl).trim(); bufTxt = bufTxt.slice(nl + 1);
        if (!line.startsWith("data:")) continue;
        const payload = line.slice(5).trim();
        if (payload === "[DONE]") continue;
        try {
          const j = JSON.parse(payload);
          const piece = (j.choices && j.choices[0] && j.choices[0].delta && j.choices[0].delta.content) || "";
          if (piece) {
            parts.push(piece); done += piece.length;
            if (done - lastReport >= DEFAULTS.progressStep) { onProgress(`  [LLM 生成中] 已生成约 ${done} 字 ...`); lastReport = done; }
          }
        } catch (e) { /* 心跳/分片边缘 */ }
      }
    }
    return parts.join("");
  }

  return { runPipeline, callLLM, DEFAULTS, apiKey, apiBase, modelName, embeddedKey };
})();
