/* Global Dossier 国外局实审分析 —— 纯浏览器静态版 core */
"use strict";

const GD = (() => {
  const HOST = "https://d1kazzu6rbodne.cloudfront.net";

  // ── 全局限速：两次请求最小间隔 + 抖动（复刻后端 RateLimiter，避免突发触发上游限流）──
  let _last = 0;
  const MIN_GAP = 0.6, JITTER = 0.8;
  async function throttle() {
    const now = performance.now();
    const wait = Math.max(0, _last + MIN_GAP * 1000 + Math.random() * JITTER * 1000 - now);
    _last = now + wait;
    if (wait > 0) await new Promise(r => setTimeout(r, wait));
  }

  // ── 简单 GET（不发任何自定义头 → 不触发 CORS 预检；上游对任意 Origin 返回 ACAO:* ）──
  async function getJSON(path, tries = 3) {
    await throttle();
    let last;
    for (let i = 0; i < tries; i++) {
      try {
        const r = await fetch(HOST + path, { method: "GET", accept: "*/*" });
        if (r.status === 429) { await sleep(2000 * (i + 1)); continue; }
        if (r.status >= 500) { last = "HTTP " + r.status; await sleep(2000 * (i + 1)); continue; }
        const t = await r.text();
        return { status: r.status, text: t };
      } catch (e) { last = String(e); await sleep(1500 * (i + 1)); }
    }
    throw new Error("请求失败 " + path + " (" + last + ")");
  }

  async function getPDF(country, num, docId, pages, tries = 2) {
    const path = `/doc-content/svc/doccontent/${country}/${num}/${docId}/${pages}/PDF`;
    await throttle();
    let last;
    for (let i = 0; i < tries; i++) {
      try {
        const r = await fetch(HOST + path, { method: "GET" });
        const buf = await r.arrayBuffer();
        const head = new Uint8Array(buf.slice(0, 5));
        if (r.status === 200 && head[0] === 0x25 && head[1] === 0x50 && head[2] === 0x44 && head[3] === 0x46) {
          return { ok: true, buf };   // %PDF
        }
        last = "not-pdf/" + r.status;
      } catch (e) { last = String(e); }
      await sleep(1200 * (i + 1));
    }
    return { ok: false, err: last };
  }

  function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

  // ── 同族 / 文书列表 ──
  async function family(cnNum) {
    const r = await getJSON(`/patent-family/svc/family/publication/CN/${cnNum}`);
    if (r.status !== 200) throw new Error("同族查询失败 " + r.status);
    return (JSON.parse(r.text).list) || [];
  }
  async function doclist(country, appNum, kind) {
    const r = await getJSON(`/doc-list/svc/doclist/${country}/${appNum}/${kind}`);
    if (r.status !== 200) throw new Error("文书列表失败 " + r.status);
    return JSON.parse(r.text);
  }

  // ══════════════ 文书挑选评分（复刻 gd_helper.pick_exam_docs）══════════════
  const FAMILY_ORDER = ["final_grant", "rejection", "citation", "applicant", "claims", "other"];
  const FAMILY_QUOTA = { final_grant: 1, rejection: 2, citation: 2, applicant: 2, claims: 1, other: 0 };
  const CODE_RANK = { NOA: 0, CTFR: 1, CTNF: 2, A01: 0, A131: 1, A523: 2, A53: 3,
    AIB21J: 4, "892": 3, SRNT: 4, SRFW: 6, CLM: 5, ISR: 4, EDREX: 3, "2004": 3,
    "2015C": 1, REM: 5, "A...": 5, AMSB: 5, IPRP: 5, RCEX: 7 };
  const NAME_RANK = [
    [0, /notice\s+of\s+allowance|decision\s+to\s+grant|登録査定|등록결정|intention\s+to\s+grant|text\s+intended\s+for\s+grant|issue\s+notification/i],
    [5, /amendment|request\s+for\s+reconsideration|written\s+opinion|意見書|答弁|보정|補正|reply|response|remarks|argument/i],
    [2, /non[- ]final|reasons?\s+(for|of)\s+refusal|拒絶理由|거절이유|notification\s+of\s+reasons|communication\s+(from|under|pursuant)/i],
    [1, /final\s+rejection|deemed\s+to\s+be\s+withdrawn|拒絶査定|거절결정|notice\s+of\s+abandonment/i],
    [3, /892|list\s+of\s+references\s+cited\s+by\s+examiner/i],
    [4, /search\s+report|search\s+strategy|search\s+information|引用文献|인용문헌/i],
    [6, /claims/i],
  ];
  function dateKey(s) {
    s = (s || "").trim();
    let m = s.match(/(\d{2})\/(\d{2})\/(\d{4})/); if (m) return [+m[3], +m[1], +m[2]];
    m = s.match(/(\d{4})-(\d{2})-(\d{2})/); if (m) return [+m[1], +m[2], +m[3]];
    return [9999, 12, 31];
  }
  function docRank(d) {
    const code = ((d.docCode || "").toUpperCase().split("-")[0]);
    const name = (d.docDesc || "").toLowerCase();
    let best = 99;
    if (code in CODE_RANK) best = Math.min(best, CODE_RANK[code]);
    for (const [rk, pat] of NAME_RANK) { if (pat.test(name)) { best = Math.min(best, rk); break; } }
    return best;
  }
  function docFamily(d) {
    const code = (d.docCode || "").toUpperCase().split("-")[0];
    const name = (d.docDesc || "").toLowerCase();
    if (/notice\s+of\s+allowance|decision\s+to\s+grant|decision\s+(on|of|for)\s+registration|登録査定|등록결정|intention\s+to\s+grant|text\s+intended\s+for\s+grant|issue\s+notification|patented/.test(name)) return "final_grant";
    if (/amendment|remarks|written\s+opinion|意見書|答弁|의견|보정|補正|request\s+for\s+continued\s+examination|request\s+for\s+reconsideration|reply|response|argument/.test(name)) return "applicant";
    if (/final\s+rejection|notice\s+of\s+abandonment|deemed\s+to\s+be\s+withdrawn|拒絶査定|거절결정|decision\s+(of|on)\s+rejection|revocat|non[- ]final|reasons?\s+(for|of)\s+refusal|拒絶理由|거절이유/.test(name)) return "rejection";
    if (/list\s+of\s+references|search\s+report|search\s+strategy|search\s+information|引用文献|인용문헌|검색/.test(name)) return "citation";
    if (code === "CLM" || /\bclaims\b/.test(name)) return "claims";
    return "other";
  }
  function sortDocs(arr) {
    arr = arr.slice().sort((a, b) => { const ka = dateKey(a.legalDateStr), kb = dateKey(b.legalDateStr); return kb[0]-ka[0]||kb[1]-ka[1]||kb[2]-ka[2]; });
    return arr.sort((a, b) => {
      const ra = docRank(a), rb = docRank(b); if (ra !== rb) return ra - rb;
      const ta = /(JP|KR)$/.test((a.docCode || "").toUpperCase()) ? 1 : 0;
      const tb = /(JP|KR)$/.test((b.docCode || "").toUpperCase()) ? 1 : 0;
      return ta - tb;
    });
  }
  function pickExamDocs(docs, maxDocs) {
    if (!docs || !docs.length) return [];
    const fam = {}; FAMILY_ORDER.forEach(f => fam[f] = []);
    docs.forEach(d => fam[docFamily(d)].push(d));
    const picked = [], used = new Set();
    for (const f of FAMILY_ORDER) {
      if (picked.length >= maxDocs) break;
      let quota = FAMILY_QUOTA[f]; if (quota <= 0) continue;
      for (const d of sortDocs(fam[f])) {
        if (picked.length >= maxDocs) break;
        const code = (d.docCode || "").toUpperCase().split("-")[0];
        if (used.has(code)) continue;
        used.add(code); picked.push(d); if (--quota <= 0) break;
      }
    }
    if (picked.length < maxDocs) {
      for (const d of sortDocs(docs)) {
        if (picked.length >= maxDocs) break;
        const code = (d.docCode || "").toUpperCase().split("-")[0];
        if (picked.includes(d) || (code && used.has(code))) continue;
        used.add(code); picked.push(d);
      }
    }
    picked.sort((a, b) => { const ka = dateKey(a.legalDateStr), kb = dateKey(b.legalDateStr); return ka[0]-kb[0]||ka[1]-kb[1]||ka[2]-kb[2]; });
    return picked;
  }

  return { family, doclist, getPDF, pickExamDocs, HOST };
})();
