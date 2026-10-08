/* 结构化提取（extract.py 的 JS 移植：对比文件/权利要求/结论/文书类型） */
"use strict";

const GDX = (() => {
  const norm = s => String(s || "").replace(/[\s,/\-－]/g, "");

  // ── 对比文件引用（[正则, 国家代码]）──
  const PATENT_NO_PATTERNS = [
    [/US[-－\s]?(?:[Pp][Uu][Bb](?:lication|LICATION)?\.?[-－\s]?)?(?:Patent\s)?(?:No\.?\s?)?(\d{4}\/\d{6,7}|[\d,]{5,13})\s?[-－]?\s?(B\d?|A1?|C\d?)?/g, "US"],
    [/US[-－\s]?(\d{1,3}(?:[-－\s]\d{3}){1,2})\s?[-－]?\s?(B\d?|A1?|C\d?)?/g, "US"],
    [/EP[-－\s]?(\d{5,9})\s?[-－]?\s?(A1?|B1?)?/g, "EP"],
    [/JP[-－\s]?(?:特開|特許|公開)?\s?(\d{4}[-－/]?\d{3,6})\s?[-－]?\s?(A|B)?/g, "JP"],
    [/(?:特開|特許公開)\s?(\d{4}[-－]?\d{3,6})/g, "JP"],
    [/KR[-－\s]?(?:공개특허\s?)?(?:제)?\s?(\d{2,4}(?:[-－\s]\d{3,8}){1,2}|\d{4,13})\s?[-－]?\s?(U\d?|Y\d?|A\d?|B\d?)?/g, "KR"],
    [/CN[-－\s]?(\d{9,12})\s?[A-Z]?/g, "CN"],
    [/WO[-－\s]?(\d{4}[\/／]\d{3,6})/g, "WO"],
    [/(?:WO|PCT)[-－\s]?(\d{4}\/?\d{6,7})/g, "WO"],
  ];
  const CATEGORY_PATTERNS = [
    /<td>\s*([XYA])\s*<\/td>/,
    /Category[：: ]*([XYA])\b/,
    /類別[：: ]*([XYA])\b/,
    /\b(X|Y|A)\b\s*(?:A\.?|B\.?|is cited|considered)/,
  ];

  function extractCitations(text, maxItems = 40, excludeNos = []) {
    const ex = new Set([...excludeNos].map(norm));
    const out = [], seen = new Set();
    for (const [pat, country] of PATENT_NO_PATTERNS) {
      pat.lastIndex = 0;
      for (const m of text.matchAll(pat)) {
        const num = norm(m[1]);
        if (!num || ex.has(num)) continue;
        const kind = m[2] || "";
        const key = country + "\u0000" + num + "\u0000" + kind;
        if (seen.has(key)) continue;
        seen.add(key);
        const ctx = text.slice(Math.max(0, m.index - 80), m.index + m[0].length + 80);
        let cat = "";
        for (const cp of CATEGORY_PATTERNS) {
          const cm = ctx.match(cp);
          if (cm) { cat = cm[1]; break; }
        }
        out.push({ no: country + " " + num, kind, category: cat,
          snippet: ctx.replace(/\s+/g, " ").trim().slice(0, 160) });
        if (out.length >= maxItems) return out;
      }
    }
    return out;
  }

  // ── 权利要求提取 ──
  const CLAIM_HEADERS = [
    /What\s+is\s+claimed\s+is\s*:/i,
    /^#*\s*\[?(?:CLAIMS|Patent\s+Claims)\]?\s*$/im,
    /Claims\s*\n?\s*1\.\s/i,
    /特許請求の範囲/,
    /청구항\s*1/,
    /权利要求\s*[1１]/,
    /^#*\s*\[Claim\s*1\]\s*$/im,
  ];
  const CLAIM_CUTS = [/\n\s*Form PTO/, /\n\s*Attorney Docket/, /\n\s*CERTIFICATE OF/,
    /\n\s*ABSTRACT/, /\n\s*[A-Z][A-Z ]{12,}\s*\n/];
  const CLAIM_REF_PATTERNS = [
    /\baccording\s+to\s+claim\s*\d/i,
    /\b(as\s+)?(?:claimed|set\s+forth|defined|recited)\s+in\s+claim\s*\d/i,
    /\bof\s+(?:any\s+)?(?:the\s+)?claims?\s*\d/i,
    /\bof\s+any\s+preceding\s+claim/i,
    /\bclaim\s*\d+\s+of\b/i,
    /請求項\s*[1-9]/,
    /請求の範囲\s*(?:第)?[1-9]/,
    /第\s*[1-9]\s*項/,
    /제\s*[1-9]\s*항/,
    /항\s*제?\s*[1-9]/,
    /(?:根据|如|按)权利要求\s*[1-9]/,
    /如請求項\s*[1-9]/,
  ];

  function extractClaims(text, maxClaims = 30, independentOnly = true) {
    for (const hdr of CLAIM_HEADERS) {
      const m = text.match(hdr);
      if (!m) continue;
      const start = m.index;
      let end = text.length;
      const rest = text.slice(start + m[0].length);
      for (const c of CLAIM_CUTS) {
        const cm = rest.match(c);
        if (cm) end = Math.min(end, start + m[0].length + cm.index);
      }
      const block = text.slice(start, end);
      const claims = [];
      const itemRe = /(?:^|\n)\s*(?:(\d{1,3})[\.\)]\s+|#*\s*\[?\s*Claim\s*(\d{1,3})\s*\]?\s*[\.:]?\s*)(.{8,6000}?)(?=\n\s*(?:\d{1,3}[\.\)]\s+|#*\s*\[?\s*Claim\s*\d{1,3}\s*\]?\s*[\.:]?\s*)|$)/gs;
      for (const cm of block.matchAll(itemRe)) {
        const num = cm[1] || cm[2];
        const claim = num + ". " + cm[3].replace(/\s+/g, " ").trim();
        if (independentOnly && CLAIM_REF_PATTERNS.some(p => p.test(claim))) continue;
        claims.push(claim);
        if (claims.length >= maxClaims) break;
      }
      if (claims.length) return claims;
    }
    return [];
  }

  // ── 审查结论判定 ──
  const GRANT_NAME = /notice\s+of\s+allowance|decision\s+to\s+grant|decision\s+(on|of|for)\s+registration|登録査定|등록결정|intention\s+to\s+grant|text\s+intended\s+for\s+grant|issue\s+notification|patented|rule\s+71\(3\)/;
  const REJECT_NAME = /final\s+rejection|拒絶査定|거절결정|decision\s+(?:of|on)\s+rejection|revocation|revoked/;
  const WITHDRAW_NAME = /notice\s+of\s+abandonment|deemed\s+to\s+be\s+withdrawn|abandoned|みなし取下げ|포기/;
  const PENDING_NAME = /non[- ]final|reasons?\s+(for|of)\s+refusal|拒絶理由|거절이유|notice\s+of\s+reasons|office\s+action|communication\s+from\s+the\s+examin/;
  const GRANT_TEXT = /notice\s+of\s+allowance|decision\s+to\s+grant|登録査定|등록결정|intention\s+to\s+grant|decision\s+(on|of|for)\s+registration|text\s+intended\s+for\s+grant/;
  const REJECT_TEXT = /final\s+rejection|decision\s+of\s+rejection|拒絶査定|거절결정/;
  const PENDING_TEXT = /claims?[^.\n]{0,80}?(?:are|is|have\s+been)\s+rejected/;
  const WITHDRAW_TEXT = /notice\s+of\s+abandonment|deemed\s+to\s+be\s+withdrawn|abandoned|みなし取下げ/;
  const AMEND_OR_SEARCH_NAME = /amendment|reply|response|remarks|written\s+opinion|search\s+report|list\s+of\s+references|意見|보정|補正|答弁|의견|search\s+strategy/;

  function detectConclusion(text, docName = "") {
    const n = (docName || "").toLowerCase();
    if (GRANT_NAME.test(n)) return "授权";
    if (PENDING_NAME.test(n)) return "审查中（已发出驳回理由）";
    if (REJECT_NAME.test(n)) return "驳回";
    if (WITHDRAW_NAME.test(n)) return "视为撤回或放弃";
    if (AMEND_OR_SEARCH_NAME.test(n)) return "审查中";
    const low = (text || "").toLowerCase().replace(/in condition for allowance/g, " ");
    if (GRANT_TEXT.test(low)) return "授权";
    if (REJECT_TEXT.test(low)) return "驳回";
    if (WITHDRAW_TEXT.test(low)) return "视为撤回或放弃";
    if (PENDING_TEXT.test(low)) return "审查中（已发出驳回理由）";
    return "审查中";
  }

  // ── 文书类型判定 ──
  const OA_PATTERNS = [/office\s*action/i, /non[- ]final/i, /final\s+rejection/i,
    /notice\s+of\s+allowance/i, /notice\s+of\s+abandonment/i,
    /intention\s+to\s+grant/i, /text\s+intended\s+for\s+grant/i,
    /notification\s+of\s+(the\s+)?reasons?\s*for\s*refusal/i,
    /decision\s+(on|of|for)\s+(registration|rejection)/i,
    /decision\s+to\s+grant/i, /reasons?\s+for\s+refusal/i,
    /communication\s+(from|under|pursuant)/i,
    /examination\s+started|examination\s+procedure/i,
    /拒絶理由通知|拒絶査定|登録査定|査定/,
    /의견제출통지|거절이유|거절결정|등록결정|결정/];
  const APPLICANT_PATTERNS = [/amendment/i, /reply/i, /response/i, /argument/i, /remarks/i,
    /written\s+opinion/i, /request\s+for\s+continued\s+examination/i,
    /request\s+for\s+reconsideration/i,
    /意見書|答弁書|補正書|手続補正|意見/, /의견서|보정서|답변서|의견/];
  const SEARCH_PATTERNS = [/search\s+report/i, /international\s+search/i, /list\s+of\s+references/i,
    /892/, /srn|srfw/i, /search\s+strategy/i, /引用文献|인용문헌|검색/];

  function detectDocType(name) {
    const n = (name || "").toLowerCase();
    if (APPLICANT_PATTERNS.some(p => p.test(n))) return "applicant";
    if (OA_PATTERNS.some(p => p.test(n))) return "office_action";
    if (SEARCH_PATTERNS.some(p => p.test(n))) return "search";
    return "other";
  }

  return { extractCitations, extractClaims, detectConclusion, detectDocType, norm };
})();
