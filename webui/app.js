(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PAGES = ["accounts", "tokens", "credits", "sessions"];

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }
  function series() {
    return [
      { key: "cache_hit_tokens", label: "缓存命中", color: cssVar("--series-cache", "#3B82B0"), cls: "cache" },
      { key: "uncached_input", label: "新输入", color: cssVar("--series-input", "#1B4F8C"), cls: "input" },
      { key: "completion_tokens", label: "输出", color: cssVar("--series-output", "#7C6F5A"), cls: "output" },
    ];
  }

  const state = {
    meta: null,
    usage: null,
    intlUsage: null,
    sessions: [],
    accounts: [],
    conversion: "",
    edition: "",
    page: "accounts",
    tokenSource: "workbuddy",
    overviewRange: "30d",
    chartRange: "30d",
    openSession: "",
    openModel: "",
    sessionTotals: null,
    sessionTimeline: null,
    creditRules: null,
    sessionChartMetric: "tokens",
    sessionChartRange: "30d",
    sessionCalls: {},
  };

  function fmt(n, d) {
    if (n === null || n === undefined || n === "") return "—";
    const x = Number(n);
    if (!isFinite(x)) return "—";
    return x.toLocaleString("zh-CN", { maximumFractionDigits: d == null ? 2 : d });
  }
  function compact(n) {
    if (n === null || n === undefined || !isFinite(Number(n))) return "—";
    const x = Number(n);
    const abs = Math.abs(x);
    if (abs >= 1e8) return (x / 1e8).toFixed(1).replace(/\.0$/, "") + "亿";
    if (abs >= 1e4) {
      const v = x / 1e4;
      return (v >= 100 ? v.toFixed(0) : v.toFixed(1).replace(/\.0$/, "")) + "万";
    }
    if (abs >= 1000) return fmt(x, 0);
    return fmt(x, x % 1 === 0 ? 0 : 2);
  }
  function pct(n) {
    if (n === null || n === undefined) return null;
    return (Number(n) * 100).toFixed(1) + "%";
  }
  function na(text) {
    return '<span class="na">' + escapeHtml(text == null ? "没读到" : text) + "</span>";
  }
  function shown(value, formatted) {
    if (value === null || value === undefined || value === "没读到" || formatted === "没读到") return na("没读到");
    return escapeHtml(formatted != null ? String(formatted) : String(value));
  }
  function when(ms) {
    if (!ms) return "—";
    const n = Number(ms);
    const sec = n > 10_000_000_000 ? n / 1000 : n;
    const dt = new Date(sec * 1000);
    if (isNaN(dt.getTime())) return "—";
    const pad = (x) => String(x).padStart(2, "0");
    return pad(dt.getMonth() + 1) + "-" + pad(dt.getDate()) + " " + pad(dt.getHours()) + ":" + pad(dt.getMinutes());
  }
  function whenFull(ms) {
    if (!ms) return "";
    const n = Number(ms);
    const sec = n > 10_000_000_000 ? n / 1000 : n;
    const dt = new Date(sec * 1000);
    if (isNaN(dt.getTime())) return "";
    const pad = (x) => String(x).padStart(2, "0");
    return (
      dt.getFullYear() + "-" + pad(dt.getMonth() + 1) + "-" + pad(dt.getDate()) +
      " " + pad(dt.getHours()) + ":" + pad(dt.getMinutes())
    );
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function todayStr() {
    const d = new Date();
    const pad = (x) => String(x).padStart(2, "0");
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }
  function shiftDay(iso, delta) {
    const p = iso.split("-").map(Number);
    const d = new Date(p[0], p[1] - 1, p[2]);
    d.setDate(d.getDate() + delta);
    const pad = (x) => String(x).padStart(2, "0");
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
  }
  function inRange(day, range) {
    if (!day) return false;
    const today = todayStr();
    if (range === "today") return day === today;
    if (range === "7d") return day >= shiftDay(today, -6) && day <= today;
    if (range === "30d") return day >= shiftDay(today, -29) && day <= today;
    if (range === "90d") return day >= shiftDay(today, -89) && day <= today;
    if (range === "month") return day.slice(0, 7) === today.slice(0, 7);
    return true;
  }
  function sumDays(days) {
    const out = {
      calls: 0, prompt_tokens: 0, completion_tokens: 0, total_tokens: 0,
      cache_hit_tokens: 0, uncached_input: 0, thinking_tokens: 0, credit: 0,
    };
    (days || []).forEach((d) => {
      Object.keys(out).forEach((k) => { out[k] += Number(d[k] || 0); });
    });
    const prompt = out.prompt_tokens;
    out.cache_hit_rate = prompt ? Math.min(out.cache_hit_tokens, prompt) / prompt : null;
    out.total_tokens = out.total_tokens || (out.prompt_tokens + out.completion_tokens);
    return out;
  }
  function emptyRow(cols, title, desc) {
    return (
      '<tr><td colspan="' + cols + '" class="empty-state">' +
      (title ? '<p class="empty-title">' + escapeHtml(title) + "</p>" : "") +
      '<p class="empty-desc">' + escapeHtml(desc) + "</p></td></tr>"
    );
  }
  function shortSource(entry) {
    const raw = entry.source_via || entry.source || "";
    const hrefCand = (entry.source && /^https?:\/\//.test(entry.source)) ? entry.source
      : (/^https?:\/\//.test(raw) ? raw : "");
    let text = raw;
    if (/ModelLink/i.test(raw)) text = "ModelLink";
    else if (/智谱|bigmodel/i.test(raw)) text = "智谱官方";
    else if (/DeepSeek|deepseek/i.test(raw)) text = "DeepSeek 官方";
    else if (/^https?:\/\//.test(raw)) {
      try {
        text = new URL(raw).hostname.replace(/^www\./, "");
      } catch (e) {
        text = raw;
      }
    } else if (raw) {
      text = raw.split(/[，,（(]/)[0].trim() || raw;
    }
    if (text.length > 24) text = text.slice(0, 22) + "…";
    return { text: text || "—", href: hrefCand || "", detail: raw, date: entry.retrieved_at || "" };
  }

  async function get(path) {
    const res = await fetch(path, { credentials: "same-origin" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.message || data.error || ("请求失败 " + res.status));
    }
    return data;
  }

  function qed(extra) {
    const p = new URLSearchParams();
    if (state.edition) p.set("edition", state.edition);
    if (state.conversion) p.set("conversion", state.conversion);
    if (extra) Object.keys(extra).forEach((k) => { if (extra[k]) p.set(k, extra[k]); });
    const s = p.toString();
    return s ? "?" + s : "";
  }

  /* ---------- 外观 ---------- */
  function resolvedTheme() {
    const explicit = document.documentElement.getAttribute("data-theme");
    if (explicit === "light" || explicit === "dark") return explicit;
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function syncThemeButtons() {
    const t = resolvedTheme();
    const light = $("theme-light");
    const dark = $("theme-dark");
    if (light) light.classList.toggle("is-on", t === "light");
    if (dark) dark.classList.toggle("is-on", t === "dark");
    if (light) light.setAttribute("aria-pressed", t === "light" ? "true" : "false");
    if (dark) dark.setAttribute("aria-pressed", t === "dark" ? "true" : "false");
  }
  function setTheme(theme) {
    if (theme === "light" || theme === "dark") {
      document.documentElement.setAttribute("data-theme", theme);
      try { localStorage.setItem("wb-theme", theme); } catch (e) {}
    } else {
      document.documentElement.removeAttribute("data-theme");
      try { localStorage.removeItem("wb-theme"); } catch (e) {}
    }
    syncThemeButtons();
    if (state.usage && state.page === "tokens") renderTokenPage();
    else if (state.usage) {
      const svg = $("trend-chart");
      if (svg && svg.childNodes.length) renderTokenPage();
    }
  }

  /* ---------- 路由 ---------- */
  function currentHashPage() {
    const h = (location.hash || "").replace(/^#\/?/, "").split("?")[0];
    return PAGES.indexOf(h) >= 0 ? h : "accounts";
  }
  function showPage(name, opts) {
    if (PAGES.indexOf(name) < 0) name = "accounts";
    state.page = name;
    document.querySelectorAll(".page").forEach((el) => {
      const on = el.getAttribute("data-page") === name;
      el.classList.toggle("is-on", on);
      el.hidden = !on;
    });
    document.querySelectorAll(".nav-item").forEach((el) => {
      el.classList.toggle("is-active", el.getAttribute("data-page") === name);
    });
    if (!opts || !opts.silent) {
      const want = "#/" + name;
      if (location.hash !== want) history.replaceState(null, "", want);
    }
    if (name === "tokens" && state.usage) {
      requestAnimationFrame(() => renderTokenPage());
    }
    if (name === "sessions") {
      requestAnimationFrame(() => renderSessionChart());
    }
  }
  window.addEventListener("hashchange", () => showPage(currentHashPage(), { silent: true }));

  /* ---------- 顶栏 / 版本 / 双版本 ---------- */
  function fillMeta(meta) {
    state.meta = meta;
    if (meta.app_version) $("app-version").textContent = "v" + meta.app_version;
    const ed = meta.edition || {};
    const notice = $("notice-panel");
    if (ed.dual) {
      notice.hidden = false;
      const opts = (ed.editions || [])
        .map((e) => {
          const mark = e.key === ed.edition ? "（当前）" : "";
          const n = e.readable
            ? (e.session_count || 0) + " 个会话 / " + (e.account_count || 0) + " 个账号"
            : "读取失败";
          return (
            "<button type='button' class='btn-ghost' data-ed='" +
            escapeHtml(e.key) + "'>" +
            escapeHtml(e.label) + " " + n + " " + mark + "</button>"
          );
        })
        .join(" ");
      notice.innerHTML = "<p>" + escapeHtml(ed.notice || "请选择要查看的版本。") + "</p><div class='actions'>" + opts + "</div>";
      notice.querySelectorAll("button[data-ed]").forEach((btn) => {
        btn.addEventListener("click", () => {
          state.edition = btn.getAttribute("data-ed");
          reload();
        });
      });
    } else {
      notice.hidden = true;
      notice.innerHTML = "";
    }
  }

  /* ---------- 账号页 ---------- */
  function accountUsage(uid) {
    const acc = { credit: 0, calls: 0, prompt: 0, completion: 0, sessions: 0 };
    (state.sessions || []).forEach((s) => {
      if ((s.user_id || "") !== uid) return;
      acc.sessions += 1;
      const u = s.usage || {};
      acc.credit += Number(u.credit || 0);
      acc.calls += Number(u.calls || 0);
      acc.prompt += Number(u.prompt_tokens || 0);
      acc.completion += Number(u.completion_tokens || 0);
    });
    return acc;
  }

  function fillAccounts(data) {
    state.accounts = (data && data.accounts) || [];
    const src = $("sync-source");
    const tgt = $("sync-target");
    const opts = state.accounts
      .map((a) => {
        const label =
          (a.nickname ? a.nickname + " · " : "") +
          a.display_id +
          "（" + a.session_count + " 个会话）" +
          (a.is_current ? " ← 当前登录" : "");
        return '<option value="' + escapeHtml(a.user_id) + '">' + escapeHtml(label) + "</option>";
      })
      .join("");
    src.innerHTML = opts;
    tgt.innerHTML = opts;
    const current = state.accounts.find((a) => a.is_current);
    const other = state.accounts.find((a) => current && a.user_id !== current.user_id) || state.accounts[0];
    if (other) src.value = other.user_id;
    if (current) tgt.value = current.user_id;

    $("account-count").textContent = String(state.accounts.length);
    const grid = $("account-grid");
    if (!state.accounts.length) {
      grid.innerHTML =
        '<div class="card empty-state"><p class="empty-title">还没有账号</p>' +
        '<p class="empty-desc">这个数据目录里还没有读到账号。</p></div>';
      return;
    }
    grid.innerHTML = state.accounts
      .map((a) => {
        const u = accountUsage(a.user_id);
        const title = a.nickname || a.display_id || "未命名账号";
        const badge = a.is_current
          ? '<span class="badge badge-on">当前登录</span>'
          : '<span class="badge">本机记录</span>';
        return (
          '<article class="account-card">' +
          '<div class="account-card-head">' +
            "<div><div class='account-name'>" + escapeHtml(title) + "</div>" +
            "<div class='account-id'>" + escapeHtml(a.display_id) + "</div></div>" +
            badge +
          "</div>" +
          '<div class="account-metric">' +
            "<div><span class='n'>" + escapeHtml(fmt(u.credit, 2)) + "</span><span class='u'>已消耗积分</span></div>" +
            "<div class='hint'>" + escapeHtml(String(a.session_count || u.sessions || 0)) + " 个会话</div>" +
          "</div>" +
          '<div class="account-meta">' +
            "<span>调用 <strong>" + escapeHtml(fmt(u.calls, 0)) + "</strong></span>" +
            "<span>输入 <strong>" + escapeHtml(compact(u.prompt)) + "</strong></span>" +
            "<span>输出 <strong>" + escapeHtml(compact(u.completion)) + "</strong></span>" +
          "</div>" +
          '<a class="account-link" href="#/sessions" data-filter="' + escapeHtml(a.display_id) + '">查看会话 →</a>' +
          "</article>"
        );
      })
      .join("");
    grid.querySelectorAll("a.account-link[data-filter]").forEach((el) => {
      el.addEventListener("click", () => {
        const f = el.getAttribute("data-filter") || "";
        $("session-filter").value = f;
        renderSessions();
      });
    });
  }

  async function fillPreflight() {
    const el = $("sync-preflight");
    const dot = $("client-dot");
    const st = $("client-status");
    try {
      const pf = await get("/api/sync/preflight" + qed());
      el.className = "preflight " + (pf.clients_running ? "bad" : pf.ok ? "ok" : "bad");
      el.textContent = pf.message || pf.error || "";
      if (pf.clients && pf.clients.length) {
        el.textContent += " 进程：" + pf.clients.map((c) => c.name).join("、") + "。";
      }
      if (pf.clients_running) {
        dot.className = "dot is-on";
        st.textContent = "WorkBuddy：运行中";
      } else {
        dot.className = "dot is-off";
        st.textContent = "WorkBuddy：未运行";
      }
    } catch (e) {
      el.className = "preflight bad";
      el.textContent = "预检失败：" + e.message;
      dot.className = "dot is-warn";
      st.textContent = "客户端：没读到";
    }
    syncReady();
  }

  function syncReady() {
    const ok =
      $("sync-yes").checked &&
      $("sync-confirm").value.trim() === (state.meta && state.meta.confirm_text) &&
      $("sync-token").value.trim() &&
      $("sync-source").value &&
      $("sync-target").value &&
      $("sync-source").value !== $("sync-target").value;
    $("sync-run").disabled = !ok;
  }

  async function doSync() {
    if ($("sync-run").disabled) return;
    const log = $("sync-log");
    log.hidden = false;
    log.textContent = "正在提交…";
    const token = $("sync-token").value.trim();
    try {
      const res = await fetch("/api/sync" + qed(), {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Authorization: "Bearer " + token,
        },
        body: JSON.stringify({
          source_uid: $("sync-source").value,
          target_uid: $("sync-target").value,
          confirm: true,
          confirm_text: $("sync-confirm").value.trim(),
          edition: state.edition,
        }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        log.textContent = (data.message || data.error || "同步失败") + (data.log ? "\n" + data.log : "");
        return;
      }
      log.textContent = (data.message || "完成") + (data.log ? "\n" + data.log : "");
      await reload();
    } catch (e) {
      log.textContent = "请求失败：" + e.message;
    }
  }

  /* ---------- 积分折算与价格 ---------- */
  function fillRates(usage) {
    const sel = $("conversion");
    const rates = usage.rates || {};
    const conversions = rates.conversions || {};
    const current = (usage.conversion && usage.conversion.key) || rates.default_conversion || "";
    sel.innerHTML = "";
    Object.keys(conversions).forEach((k) => {
      const c = conversions[k];
      const opt = document.createElement("option");
      opt.value = k;
      opt.textContent = c.label + (c.available ? "" : "（没读到）");
      if (k === current) opt.selected = true;
      sel.appendChild(opt);
    });
    state.conversion = current;
    const conv = usage.conversion;
    const note = [];
    if (conv) {
      note.push(
        "估算费用按「" + conv.label + "」" +
        (conv.available ? "，每积分 " + conv.cny_per_credit + " 元。" : "，该折算不可用，标为没读到。") +
        " 来源：" + (conv.source_title || conv.source) +
        (conv.retrieved_at ? "（取自 " + conv.retrieved_at + "）" : "") +
        (conv.note ? " " + conv.note : "")
      );
    }
    (usage.notes || []).forEach((n) => note.push(n));
    $("rate-note").textContent = note.join(" ");
  }

  function fillStats(usage) {
    const t = usage.totals || {};
    const items = [
      ["已消耗积分", t.credit != null, fmt(t.credit, 2)],
      ["套餐折算", t.estimated_cny != null, t.estimated_cny == null ? "没读到" : "¥ " + fmt(t.estimated_cny, 2)],
      ["API 等效", t.equivalent_cny != null, t.equivalent_cny == null ? "没读到" : "¥ " + fmt(t.equivalent_cny, 2)],
      ["缓存命中率", t.cache_hit_rate != null, pct(t.cache_hit_rate) || "没读到"],
    ];
    $("stats").innerHTML = items
      .map(([l, ok, n]) =>
        '<div class="metric"><div class="k">' + escapeHtml(l) + '</div><div class="v">' +
        (ok ? escapeHtml(n) : na("没读到")) + "</div></div>"
      )
      .join("");
  }

  function fillInfer(usage) {
    const tb = $("infer-table").querySelector("tbody");
    const rows = usage.by_model || [];
    if (!rows.length) {
      tb.innerHTML = emptyRow(7, "还没有反推结果", "这个数据目录里还没有读到 rawUsage。");
      return;
    }
    tb.innerHTML = rows
      .map((r) => {
        const inf = r.inference || {};
        const ok = inf.status === "ok" && inf.tokens_per_credit;
        const tpc = ok ? inf.tokens_per_credit : null;
        const cell = (k) => (!tpc || tpc[k] == null ? na("没读到") : escapeHtml(fmt(tpc[k], 0)));
        const status = ok
          ? '<span class="status-pill is-ok">已反推</span>'
          : '<span class="status-pill is-miss">没读到</span>';
        return (
          "<tr>" +
          "<td class='clip' title='" + escapeHtml(r.model) + "'>" + escapeHtml(r.model) + "</td>" +
          '<td class="num">' + fmt(inf.calls != null ? inf.calls : r.calls, 0) + "</td>" +
          "<td>" + status + "</td>" +
          '<td class="num">' + cell("uncached_input") + "</td>" +
          '<td class="num">' + cell("cache_hit") + "</td>" +
          '<td class="num">' + cell("output") + "</td>" +
          "<td class='note' title='" + escapeHtml(inf.reason || "") + "'>" +
            escapeHtml(inf.reason || (ok ? "三项分开计算，未使用默认汇率。" : "")) + "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function fillModels(usage) {
    const tb = $("model-table").querySelector("tbody");
    const rows = usage.by_model || [];
    if (!rows.length) {
      tb.innerHTML = emptyRow(7, "还没有用量", "这个数据目录里还没有读到 rawUsage。");
      const bar = $("cross-check-bar");
      if (bar) bar.hidden = true;
      return;
    }
    tb.innerHTML = rows
      .map((r) => {
        const mul =
          r.official_multiplier_status === "没读到" || r.official_multiplier == null
            ? null
            : "x" + r.official_multiplier;
        const cny =
          r.estimated_cny_status === "没读到" || r.estimated_cny == null
            ? null
            : "¥ " + fmt(r.estimated_cny, 2);
        const inf = r.inference || {};
        const tpc = (inf.status === "ok" && inf.tokens_per_credit) ? inf.tokens_per_credit : null;
        const cellInf = (k) => {
          if (!tpc || tpc[k] == null) return na("没读到");
          return escapeHtml(fmt(tpc[k], 0));
        };
        const eq = (r.equivalent_cny_status === "没读到" || r.equivalent_cny == null)
          ? null : "¥ " + fmt(r.equivalent_cny, 2);
        const hit = pct(r.cache_hit_rate);
        const open = state.openModel === r.model;
        const detail =
          "<tr class='model-detail' data-for='" + escapeHtml(r.model) + "'" + (open ? "" : " hidden") + ">" +
          "<td colspan='7'><div class='detail-grid'>" +
          "<div><div class='k'>输入 token</div><div class='v'>" + escapeHtml(fmt(r.prompt_tokens, 0)) + "</div></div>" +
          "<div><div class='k'>输出 token</div><div class='v'>" + escapeHtml(fmt(r.completion_tokens, 0)) + "</div></div>" +
          "<div><div class='k'>思考 token</div><div class='v'>" + escapeHtml(fmt(r.thinking_tokens, 0)) + "</div></div>" +
          "<div><div class='k'>去缓存效率</div><div class='v'>" +
            (r.tokens_per_credit_uncached == null ? na("没读到") : escapeHtml(fmt(r.tokens_per_credit_uncached, 0))) +
          "</div></div>" +
          "<div><div class='k'>每积分·未命中</div><div class='v'>" + cellInf("uncached_input") + "</div></div>" +
          "<div><div class='k'>每积分·缓存</div><div class='v'>" + cellInf("cache_hit") + "</div></div>" +
          "<div><div class='k'>每积分·输出</div><div class='v'>" + cellInf("output") + "</div></div>" +
          "<div><div class='k'>官方倍率</div><div class='v'>" + (mul == null ? na("没读到") : escapeHtml(mul)) + "</div></div>" +
          "</div></td></tr>";
        return (
          "<tr class='model-row" + (open ? " is-open" : "") + "' data-model='" + escapeHtml(r.model) + "'>" +
          "<td class='clip' title='" + escapeHtml(r.model) + "'>" + escapeHtml(r.model) + "</td>" +
          '<td class="num">' + fmt(r.calls, 0) + "</td>" +
          '<td class="num">' + fmt(r.credit, 2) + "</td>" +
          '<td class="num" title="' + escapeHtml(r.wb_rule_expected_note || "") + '">' +
            (r.wb_rule_expected_status === "ok" && r.wb_rule_expected_credit != null
              ? escapeHtml(fmt(r.wb_rule_expected_credit, 2))
              : na("没读到")) + "</td>" +
          '<td class="num">' + (cny == null ? na("没读到") : escapeHtml(cny)) + "</td>" +
          '<td class="num">' + (eq == null ? na("没读到") : escapeHtml(eq)) + "</td>" +
          '<td class="num">' + (hit == null ? na("没读到") : escapeHtml(hit)) + "</td>" +
          "</tr>" +
          detail
        );
      })
      .join("");
    tb.querySelectorAll("tr.model-row").forEach((tr) => {
      tr.addEventListener("click", () => {
        const id = tr.getAttribute("data-model");
        state.openModel = state.openModel === id ? "" : id;
        fillModels(state.usage || {});
      });
    });
    const cc = usage.cross_check || {};
    const bar = $("cross-check-bar");
    const text = $("cross-check");
    const cdot = $("cross-check-dot");
    if (bar && text) {
      if (cc.summary) {
        bar.hidden = false;
        text.textContent = cc.summary;
        if (cdot) {
          const ok = /一致|通过|吻合/.test(cc.summary) && !/不一致|失败|差异/.test(cc.summary);
          const bad = /不一致|失败/.test(cc.summary);
          cdot.className = "dot " + (ok ? "is-on" : bad ? "is-warn" : "is-off");
        }
      } else {
        bar.hidden = true;
        text.textContent = "";
      }
    }
  }

  function fillPrices(usage) {
    const rates = (usage && usage.rates) || {};
    const prices = (rates.api_unit_prices || {}).models || [];
    const tb = document.querySelector("#price-table tbody");
    if (tb) {
      if (!prices.length) {
        tb.innerHTML = emptyRow(5, "还没有单价", "价格表里还没有按量单价。");
      } else {
        tb.innerHTML = prices.map((e) => {
          const miss = e.status === "没读到";
          const cell = (k) => (miss || e[k] == null || e[k] === "没读到") ? na("没读到") : escapeHtml(fmt(e[k], 4));
          const src = shortSource(e);
          const tip = src.detail || src.text;
          const srcHtml = src.href
            ? '<a href="' + escapeHtml(src.href) + '" target="_blank" rel="noopener noreferrer" title="' +
              escapeHtml(tip) + '">' + escapeHtml(src.text) + "</a>"
            : '<span title="' + escapeHtml(tip) + '">' + escapeHtml(src.text) + "</span>";
          const dateHtml = src.date ? '<span class="when">' + escapeHtml(src.date) + "</span>" : "";
          return "<tr><td class='clip' title='" + escapeHtml(e.id || "") + "'>" + escapeHtml(e.id || "") + "</td>" +
            '<td class="num">' + cell("input") + "</td>" +
            '<td class="num">' + cell("output") + "</td>" +
            '<td class="num">' + cell("cached_read") + "</td>" +
            "<td class='src'>" + srcHtml + dateHtml + "</td></tr>";
        }).join("");
      }
    }
    const afp = rates.volc_afp || {};
    const atb = document.querySelector("#afp-table tbody");
    const models = afp.models || {};
    const names = Object.keys(models);
    if (atb) {
      if (!names.length) {
        atb.innerHTML = emptyRow(4, "还没有系数", "没有已核对的 AFP 系数。");
      } else {
        atb.innerHTML = names.map((n) => {
          const m = models[n] || {};
          const miss = m.status === "没读到";
          const noteRaw = m.note || "";
          const note = ((m.limited_offer && noteRaw.indexOf("限时折扣") < 0) ? "限时折扣 " : "") + noteRaw;
          return "<tr><td class='clip' title='" + escapeHtml(n) + "'>" + escapeHtml(n) + "</td>" +
            '<td class="num">' + (miss || m.input_coef == null ? na("没读到") : escapeHtml(String(m.input_coef))) + "</td>" +
            '<td class="num">' + (miss || m.output_coef == null ? na("没读到") : escapeHtml(String(m.output_coef))) + "</td>" +
            "<td class='note' title='" + escapeHtml(note) + "'>" + escapeHtml(note) + "</td></tr>";
        }).join("");
      }
    }
    const f = $("afp-formula");
    if (f) {
      f.textContent = (afp.formula || "") +
        (afp.source ? " 来源：" + afp.source : "") +
        (afp.retrieved_at ? "（取自 " + afp.retrieved_at + "）" : "");
    }
  }

  /* ---------- Token 统计 ---------- */
  function activeUsageBundle() {
    if (state.tokenSource === "codebuddy") {
      const cb = (state.usage && state.usage.codebuddy) || {};
      return {
        totals: cb.totals || {},
        by_day: cb.by_day || [],
        status: cb.status,
        reason: cb.reason || "",
        label: "CodeBuddy CLI",
        generated: state.usage && state.usage.generated_at,
      };
    }
    if (state.tokenSource === "intl") {
      const u = state.intlUsage;
      if (!u) {
        return { totals: {}, by_day: [], status: "没读到", reason: "没读到国际版用量。", label: "WorkBuddy 国际版" };
      }
      return {
        totals: u.totals || {},
        by_day: u.by_day || [],
        status: "ok",
        reason: "",
        label: "WorkBuddy 国际版",
        generated: u.generated_at,
      };
    }
    const u = state.usage || {};
    return {
      totals: u.totals || {},
      by_day: u.by_day || [],
      status: "ok",
      reason: "",
      label: "WorkBuddy",
      generated: u.generated_at,
    };
  }

  function fillTokenTabs() {
    const ed = (state.meta && state.meta.edition) || {};
    const tabs = [
      { id: "workbuddy", label: ed.edition === "intl" ? "WorkBuddy 国际版" : "WorkBuddy" },
    ];
    if (ed.dual && ed.edition !== "intl") {
      tabs.push({ id: "intl", label: "WorkBuddy 国际版" });
    }
    tabs.push({ id: "codebuddy", label: "CodeBuddy CLI" });
    if (tabs.every((t) => t.id !== state.tokenSource)) state.tokenSource = "workbuddy";
    const box = $("token-tabs");
    box.innerHTML = tabs
      .map((t) =>
        "<button type='button' role='tab' data-src='" + t.id + "'" +
        (t.id === state.tokenSource ? " class='is-on' aria-selected='true'" : " aria-selected='false'") +
        ">" + escapeHtml(t.label) + "</button>"
      )
      .join("");
    box.querySelectorAll("button[data-src]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.tokenSource = btn.getAttribute("data-src");
        renderTokenPage();
      });
    });
  }

  function renderTokenPage() {
    fillTokenTabs();
    const bundle = activeUsageBundle();
    const updated = $("token-updated");
    if (bundle.generated) {
      updated.textContent = "当前数据更新于 " + bundle.generated + "（UTC+8）";
    } else {
      updated.textContent = "正在读取本机用量…";
    }

    const daysAll = bundle.by_day || [];
    const daysOverview = daysAll.filter((d) => inRange(d.day, state.overviewRange));
    const useTotals = state.overviewRange === "all" || !daysAll.length;
    const totals = useTotals ? (bundle.totals || {}) : sumDays(daysOverview);
    const SERIES = series();

    if (bundle.status === "没读到" && state.tokenSource === "codebuddy") {
      $("comp-bar").innerHTML = "";
      $("comp-legend").innerHTML = "";
      $("token-metrics").innerHTML =
        '<div class="metric span-all"><div class="empty-state">' +
        '<p class="empty-title">没读到 CodeBuddy CLI</p>' +
        '<p class="empty-desc">' + escapeHtml(bundle.reason || "没读到") + "</p></div></div>";
      renderChart([]);
      return;
    }

    const parts = SERIES.map((s) => ({
      key: s.key,
      label: s.label,
      color: s.color,
      cls: s.cls,
      value: Number(totals[s.key] || 0),
    }));
    const sum = parts.reduce((a, p) => a + p.value, 0);
    const bar = $("comp-bar");
    bar.innerHTML = "";
    if (sum) {
      parts.forEach((p) => {
        const el = document.createElement("span");
        el.className = "seg-" + p.cls;
        el.style.width = ((p.value / sum) * 100).toFixed(3) + "%";
        el.title = p.label;
        bar.appendChild(el);
      });
    }
    $("comp-legend").innerHTML = parts
      .map((p) => {
        const share = sum ? ((p.value / sum) * 100).toFixed(1) + "%" : null;
        return '<span><i class="swatch swatch-' + p.cls + '"></i>' +
          escapeHtml(p.label) + " " + (share == null ? na("没读到") : escapeHtml(share)) + "</span>";
      })
      .join("");

    const inputTok = totals.prompt_tokens != null ? totals.prompt_tokens : (totals.uncached_input || 0) + (totals.cache_hit_tokens || 0);
    const totalTok = totals.total_tokens || (inputTok + (totals.completion_tokens || 0));
    const hit = totals.cache_hit_rate != null
      ? totals.cache_hit_rate
      : (inputTok ? Math.min(totals.cache_hit_tokens || 0, inputTok) / inputTok : null);
    const hitText = pct(hit);
    const metrics = [
      ["总 Token", compact(totalTok), totalTok != null && isFinite(Number(totalTok))],
      ["输入 Token", compact(inputTok), inputTok != null && isFinite(Number(inputTok))],
      ["输出 Token", compact(totals.completion_tokens), totals.completion_tokens != null],
      ["缓存命中率", hitText || "没读到", hitText != null],
    ];
    $("token-metrics").innerHTML = metrics
      .map(([l, n, ok]) =>
        '<div class="metric"><div class="k">' + escapeHtml(l) + '</div><div class="v">' +
        (ok ? escapeHtml(n) : na("没读到")) + "</div></div>"
      )
      .join("");

    const chartDays = daysAll.filter((d) => inRange(d.day, state.chartRange));
    renderChart(chartDays);
  }

  function niceMax(v) {
    if (v <= 0) return 1;
    const exp = Math.pow(10, Math.floor(Math.log10(v)));
    const n = v / exp;
    const nice = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return nice * exp;
  }

  function renderChart(days) {
    const svg = $("trend-chart");
    const empty = $("chart-empty");
    const tip = $("chart-tip");
    const legend = $("chart-legend");
    const SERIES = series();
    const axis = cssVar("--chart-axis", "#94A3B8");
    const gridC = cssVar("--chart-grid", "#E2E8F0");
    const surface = cssVar("--surface", "#FFFFFF");
    const callsC = cssVar("--series-calls", "#94A3B8");
    legend.innerHTML =
      SERIES.map((s) => '<span><i class="swatch swatch-' + s.cls + '"></i>' + escapeHtml(s.label) + "</span>").join("") +
      '<span><i class="swatch swatch-calls"></i>调用次数</span>';

    if (!days || !days.length) {
      svg.innerHTML = "";
      empty.hidden = false;
      empty.innerHTML = '<p class="empty-title">没读到按日用量</p><p class="empty-desc">该时间段没有按日的用量数据。</p>';
      tip.hidden = true;
      return;
    }
    empty.hidden = true;

    const box = $("chart-box");
    const W = Math.max(box.clientWidth || 720, 480);
    const H = Math.max(box.clientHeight || 300, 240);
    const pad = { l: 56, r: 48, t: 10, b: 36 };
    const innerW = W - pad.l - pad.r;
    const innerH = H - pad.t - pad.b;
    const maxTok = niceMax(Math.max(0, ...days.map((d) =>
      Number(d.cache_hit_tokens || 0) + Number(d.uncached_input || 0) + Number(d.completion_tokens || 0)
    )));
    const maxCalls = niceMax(Math.max(0, ...days.map((d) => Number(d.calls || 0))));
    const n = days.length;
    const slot = innerW / n;
    const bw = Math.min(36, Math.max(6, slot * 0.45));
    const xAt = (i) => pad.l + i * slot + (slot - bw) / 2;
    const yTok = (v) => pad.t + innerH - (v / maxTok) * innerH;
    const yCall = (v) => pad.t + innerH - (v / maxCalls) * innerH;

    const ticks = 4;
    let grid = "";
    for (let i = 0; i <= ticks; i++) {
      const v = maxTok * (i / ticks);
      const y = yTok(v);
      grid +=
        '<line x1="' + pad.l + '" x2="' + (W - pad.r) + '" y1="' + y + '" y2="' + y +
        '" stroke="' + gridC + '" stroke-width="1"/>' +
        '<text x="' + (pad.l - 8) + '" y="' + (y + 4) + '" text-anchor="end" fill="' + axis +
        '" font-size="11" font-family="ui-monospace, monospace">' +
        escapeHtml(compact(v)) + "</text>";
    }
    for (let i = 0; i <= ticks; i++) {
      const v = maxCalls * (i / ticks);
      const y = yCall(v);
      grid +=
        '<text x="' + (W - pad.r + 8) + '" y="' + (y + 4) + '" text-anchor="start" fill="' + axis +
        '" font-size="11" font-family="ui-monospace, monospace">' +
        escapeHtml(String(Math.round(v))) + "</text>";
    }

    let bars = "";
    const pts = [];
    days.forEach((d, i) => {
      const x = xAt(i);
      let y = pad.t + innerH;
      const segs = [];
      SERIES.forEach((s) => {
        const v = Number(d[s.key] || 0);
        const h = (v / maxTok) * innerH;
        if (h > 0) segs.push({ s: s, h: h });
      });
      segs.forEach((seg, si) => {
        y -= seg.h;
        const top = si === segs.length - 1;
        const rx = top ? Math.min(3, bw / 2) : 0;
        bars +=
          '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + bw.toFixed(1) +
          '" height="' + Math.max(seg.h, 0.5).toFixed(1) + '" fill="' + seg.s.color +
          '" rx="' + rx + '" ry="' + rx + '" data-i="' + i + '"/>';
        if (top && rx) {
          const cover = Math.min(rx + 0.5, Math.max(seg.h - 0.5, 0));
          if (cover > 0 && seg.h > rx) {
            bars +=
              '<rect x="' + x.toFixed(1) + '" y="' + (y + rx).toFixed(1) + '" width="' + bw.toFixed(1) +
              '" height="' + (seg.h - rx).toFixed(1) + '" fill="' + seg.s.color + '" data-i="' + i + '"/>';
          }
        }
      });
      const cx = x + bw / 2;
      const cy = yCall(Number(d.calls || 0));
      pts.push(cx.toFixed(1) + "," + cy.toFixed(1));
      const label = (d.day || "").slice(5);
      if (n <= 16 || i % Math.ceil(n / 12) === 0 || i === n - 1) {
        bars +=
          '<text x="' + cx.toFixed(1) + '" y="' + (H - 12) + '" text-anchor="middle" fill="' + axis +
          '" font-size="11">' +
          escapeHtml(label) + "</text>";
      }
    });

    const line =
      '<polyline fill="none" stroke="' + callsC + '" stroke-width="1.6" stroke-dasharray="4 3" points="' +
      pts.join(" ") + '"/>' +
      pts
        .map((p) => {
          const [cx, cy] = p.split(",");
          return '<circle cx="' + cx + '" cy="' + cy + '" r="2.2" fill="' + surface +
            '" stroke="' + callsC + '" stroke-width="1.4"/>';
        })
        .join("");

    const hitboxes = days
      .map((d, i) => {
        const x = pad.l + i * (innerW / n);
        return (
          '<rect class="hit" data-i="' + i + '" x="' + x.toFixed(1) + '" y="' + pad.t +
          '" width="' + (innerW / n).toFixed(1) + '" height="' + innerH +
          '" fill="transparent"/>'
        );
      })
      .join("");

    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.innerHTML = grid + bars + line + hitboxes;

    svg.onmousemove = (ev) => {
      const t = ev.target;
      const i = t && t.getAttribute && t.getAttribute("data-i");
      if (i == null) { tip.hidden = true; return; }
      const d = days[Number(i)];
      if (!d) { tip.hidden = true; return; }
      const rows = SERIES.map((s) =>
        "<div>" + escapeHtml(s.label) + "　<b>" + escapeHtml(fmt(d[s.key] || 0, 0)) + "</b></div>"
      ).join("");
      tip.innerHTML =
        "<b>" + escapeHtml(d.day) + "</b>" +
        rows +
        "<div>调用　<b>" + escapeHtml(fmt(d.calls || 0, 0)) + "</b></div>";
      tip.hidden = false;
      const rect = box.getBoundingClientRect();
      let left = ev.clientX - rect.left + 12;
      let top = ev.clientY - rect.top + 12;
      if (left + 180 > rect.width) left = ev.clientX - rect.left - 180;
      if (top + 120 > rect.height) top = ev.clientY - rect.top - 110;
      tip.style.left = left + "px";
      tip.style.top = top + "px";
    };
    svg.onmouseleave = () => { tip.hidden = true; };
  }

  /* ---------- 会话 ---------- */
  function fmtCny(n) {
    if (n === null || n === undefined || !isFinite(Number(n))) return null;
    const x = Number(n);
    const abs = Math.abs(x);
    let d = 2;
    if (abs > 0 && abs < 0.01) d = 6;
    else if (abs < 1) d = 4;
    return "¥ " + x.toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function officialCell(u) {
    const st = u.official_cny_status;
    if ((st === "ok" || st === "partial") && u.official_cny != null) {
      const cls = st === "partial" ? "official-partial" : "official-ok";
      const tag = st === "partial" ? '<span class="tag-partial">部分</span>' : "";
      const tip = u.official_cny_note || "";
      return '<span class="' + cls + '" title="' + escapeHtml(tip) + '">' +
        escapeHtml(fmtCny(u.official_cny)) + tag + "</span>";
    }
    return na("没读到");
  }

  function cacheCell(tokens, rate) {
    const tok = compact(tokens || 0);
    const r = rate == null ? "" : pct(rate);
    return (
      '<div class="cache-cell" title="缓存命中 ' + escapeHtml(fmt(tokens || 0, 0)) +
        (r ? "，命中率 " + r : "") + '">' +
      '<span>' + escapeHtml(tok) + "</span>" +
      (r ? '<span class="rate">' + escapeHtml(r) + "</span>" : "") +
      "</div>"
    );
  }

  function periodTag(period) {
    if (period === "peak") return '<span class="tag-peak">高峰</span>';
    if (period && period.indexOf("offpeak") === 0) return '<span class="tag-offpeak">闲时</span>';
    if (period === "standard") return '<span class="tag-standard">标准</span>';
    return "";
  }

  function totalsFromApi() {
    const u = (state.sessionTotals && state.sessionTotals.usage) || {};
    const o = (state.sessionTotals && state.sessionTotals.official) || {};
    return { u: u, o: o };
  }

  function renderSessionSummary(filteredRows, isFilter) {
    const box = $("session-summary");
    const noteEl = $("session-price-note");
    if (!box) return;
    let tokens = 0, prompt = 0, uncached = 0, hit = 0, output = 0, credit = 0;
    let cny = null, cnyPeak = 0, cnyOff = 0, cnyStd = 0, saved = null;
    let priced = false, missingCount = 0, missingModels = [];
    let sessionCount = 0, scopeLabel = "";
    let hitRate = null;
    let callsPeak = 0, callsOff = 0;
    let periodNote = "";

    if (!isFilter) {
      const { u, o } = totalsFromApi();
      tokens = Number(u.total_tokens || o.total_tokens || 0);
      prompt = Number(u.prompt_tokens || o.prompt_tokens || 0);
      uncached = Number(u.uncached_input || o.uncached_input || 0);
      hit = Number(u.cache_hit_tokens || o.cache_hit_tokens || 0);
      output = Number(u.completion_tokens || o.completion_tokens || 0);
      credit = Number(u.credit || o.credit || 0);
      sessionCount = Number(u.session_count || o.session_count || (filteredRows || []).length);
      scopeLabel = "全部 " + sessionCount + " 个会话";
      hitRate = u.cache_hit_rate != null ? u.cache_hit_rate : (o.cache_hit_rate != null ? o.cache_hit_rate : (prompt ? Math.min(hit, prompt) / prompt : null));
      if ((o.official_cny_status === "ok" || o.official_cny_status === "partial") && o.official_cny != null) {
        cny = Number(o.official_cny);
        priced = true;
        cnyPeak = Number(o.official_cny_peak || 0);
        cnyOff = Number(o.official_cny_offpeak || 0);
        cnyStd = Number(o.official_cny_standard || 0);
        saved = o.cache_hit_saved != null ? Number(o.cache_hit_saved) : null;
        callsPeak = o.official_cny_calls_peak || 0;
        callsOff = o.official_cny_calls_offpeak || 0;
      }
      missingCount = o.official_cny_missing_count || (o.official_cny_missing_models || []).length;
      missingModels = o.official_cny_missing_models || [];
      periodNote = o.official_cny_period_note || "";
    } else {
      const rows = filteredRows || [];
      if (!rows.length) {
        box.innerHTML = "";
        if (noteEl) noteEl.textContent = "";
        return;
      }
      sessionCount = rows.length;
      scopeLabel = "筛选后 " + sessionCount + " 个会话";
      let cnySum = 0, savedSum = 0, savedOk = false;
      const missingSet = {};
      rows.forEach((s) => {
        const u = s.usage || {};
        tokens += Number(u.total_tokens || ((u.prompt_tokens || 0) + (u.completion_tokens || 0)));
        prompt += Number(u.prompt_tokens || 0);
        uncached += Number(u.uncached_input || 0);
        hit += Number(u.cache_hit_tokens || 0);
        output += Number(u.completion_tokens || 0);
        credit += Number(u.credit || 0);
        const st = u.official_cny_status;
        if ((st === "ok" || st === "partial") && u.official_cny != null) {
          cnySum += Number(u.official_cny);
          priced = true;
          cnyPeak += Number(u.official_cny_peak || 0);
          cnyOff += Number(u.official_cny_offpeak || 0);
          cnyStd += Number(u.official_cny_standard || 0);
          if (u.cache_hit_saved != null) { savedSum += Number(u.cache_hit_saved); savedOk = true; }
        }
        missingCount += Number(u.official_cny_missing_count || (u.official_cny_missing_models || []).length || 0);
        (u.official_cny_missing_models || []).forEach((m) => { missingSet[m] = 1; });
      });
      cny = priced ? cnySum : null;
      saved = savedOk ? savedSum : null;
      missingModels = Object.keys(missingSet);
      hitRate = prompt ? Math.min(hit, prompt) / prompt : null;
    }

    const officialSub = [];
    if (priced) {
      const parts = [];
      if (cnyPeak) parts.push("高峰 " + fmtCny(cnyPeak));
      if (cnyOff) parts.push("闲时 " + fmtCny(cnyOff));
      if (cnyStd) parts.push("标准 " + fmtCny(cnyStd));
      if (parts.length) officialSub.push(parts.join(" · "));
      if (callsPeak || callsOff) officialSub.push("高峰 " + callsPeak + " 次，闲时 " + callsOff + " 次");
      if (saved != null) officialSub.push("缓存命中省 " + fmtCny(saved));
    }

    const cards = [
      { k: "总 Token", v: compact(tokens), s: scopeLabel },
      { k: "输入", v: compact(prompt), s: "未命中 " + compact(uncached) },
      { k: "缓存命中", v: compact(hit), s: hitRate == null ? "" : "命中率 " + pct(hitRate), cache: true },
      { k: "输出", v: compact(output), s: "" },
      { k: "积分", v: fmt(credit, 2), s: "" },
      {
        k: "官方价",
        v: priced ? fmtCny(cny) : "没读到",
        s: officialSub.join("\n"),
        sHtml: officialSub.map(function (line, i) {
          const cls = line.indexOf("缓存命中省") === 0 ? " s-save" : "";
          return '<div class="s' + cls + '">' + escapeHtml(line) + "</div>";
        }).join(""),
        miss: !priced,
      },
    ];
    box.innerHTML = cards.map((c) => {
      const vhtml = c.miss
        ? '<div class="v">' + na("没读到") + "</div>"
        : '<div class="v" title="' + escapeHtml(String(c.v)) + '">' + escapeHtml(String(c.v)) + "</div>";
      const shtml = c.sHtml
        ? c.sHtml
        : (c.s ? '<div class="s">' + escapeHtml(c.s) + "</div>" : "");
      return (
        '<div class="metric' + (c.cache ? " is-cache" : "") + '">' +
        '<div class="k">' + escapeHtml(c.k) + "</div>" +
        vhtml +
        shtml +
        "</div>"
      );
    }).join("");
    if (noteEl) {
      const bits = [];
      if (missingCount) {
        const extra = missingModels.length ? "（" + missingModels.join("、") + "）" : "";
        bits.push((isFilter ? "筛选范围内 " : "") + missingCount + " 个模型没读到价格，未计入" + extra + "。");
      }
      if (periodNote) bits.push(periodNote);
      else if (priced) bits.push("官方价按每次调用的北京时间区分 DeepSeek 高峰和闲时；没有高峰价的模型用标准价。");
      if (saved != null) bits.push("缓存命中省下的费用 = 这批命中 token 若按未命中输入单价计，会多出的差额。只统计已读到价格的调用。");
      if (isFilter) bits.push("上面数字是当前筛选结果，不是全部会话。");
      noteEl.textContent = bits.join(" ");
    }
  }

  function renderSessionChart() {
    const svg = $("session-chart");
    const empty = $("session-chart-empty");
    const tip = $("session-chart-tip");
    const legend = $("session-chart-legend");
    const box = $("session-chart-box");
    if (!svg || !box) return;
    const daysAll = ((state.sessionTimeline || {}).by_day) || [];
    const days = daysAll.filter((d) => inRange(d.day, state.sessionChartRange));
    const metric = state.sessionChartMetric || "tokens";
    const axis = cssVar("--chart-axis", "#94A3B8");
    const gridC = cssVar("--chart-grid", "#E2E8F0");
    let SERIES;
    if (metric === "credit") {
      SERIES = [{ key: "credit", label: "积分", color: cssVar("--brand", "#22c55e"), cls: "cache" }];
    } else if (metric === "official") {
      SERIES = [
        { key: "official_cny_peak", label: "高峰", color: cssVar("--amber", "#d97706"), cls: "output" },
        { key: "official_cny_offpeak", label: "闲时", color: cssVar("--series-input", "#0d9488"), cls: "input" },
        { key: "official_cny_standard", label: "标准价", color: cssVar("--series-calls", "#64748b"), cls: "calls" },
      ];
    } else {
      SERIES = series();
    }
    legend.innerHTML = SERIES.map((s) =>
      '<span><i class="swatch swatch-' + s.cls + '"></i>' + escapeHtml(s.label) + "</span>"
    ).join("") +
      (metric === "official"
        ? '<span><i class="swatch swatch-cache"></i>缓存命中省下（提示里）</span>'
        : "");

    if (!days.length) {
      svg.innerHTML = "";
      empty.hidden = false;
      empty.innerHTML = '<p class="empty-title">没读到按日用量</p><p class="empty-desc">该时间段没有按日的用量数据。</p>';
      if (tip) tip.hidden = true;
      return;
    }
    empty.hidden = true;
    const W = Math.max(box.clientWidth || 720, 480);
    const H = Math.max(box.clientHeight || 300, 240);
    const pad = { l: 56, r: 16, t: 10, b: 36 };
    const innerW = W - pad.l - pad.r;
    const innerH = H - pad.t - pad.b;
    const stackOf = (d) => SERIES.reduce((a, s) => a + Number(d[s.key] || 0), 0);
    const maxV = niceMax(Math.max(0, ...days.map(stackOf)));
    const n = days.length;
    const slot = innerW / n;
    const bw = Math.min(36, Math.max(6, slot * 0.45));
    const xAt = (i) => pad.l + i * slot + (slot - bw) / 2;
    const yV = (v) => pad.t + innerH - (maxV ? (v / maxV) * innerH : 0);
    const ticks = 4;
    let grid = "";
    const tickFmt = metric === "official" ? (v) => (v ? fmtCny(v) : "¥ 0") : (v) => (metric === "credit" ? fmt(v, 1) : compact(v));
    for (let i = 0; i <= ticks; i++) {
      const v = maxV * (i / ticks);
      const y = yV(v);
      grid +=
        '<line x1="' + pad.l + '" x2="' + (W - pad.r) + '" y1="' + y + '" y2="' + y +
        '" stroke="' + gridC + '" stroke-width="1"/>' +
        '<text x="' + (pad.l - 8) + '" y="' + (y + 4) + '" text-anchor="end" fill="' + axis +
        '" font-size="11" font-family="ui-monospace, monospace">' +
        escapeHtml(String(tickFmt(v))) + "</text>";
    }
    let bars = "";
    days.forEach((d, i) => {
      const x = xAt(i);
      let y = pad.t + innerH;
      SERIES.forEach((s) => {
        const v = Number(d[s.key] || 0);
        const h = maxV ? (v / maxV) * innerH : 0;
        if (h <= 0) return;
        y -= h;
        bars +=
          '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + bw.toFixed(1) +
          '" height="' + Math.max(h, 0.5).toFixed(1) + '" fill="' + s.color +
          '" rx="2" data-i="' + i + '"/>';
      });
      const cx = x + bw / 2;
      const label = (d.day || "").slice(5);
      if (n <= 16 || i % Math.ceil(n / 12) === 0 || i === n - 1) {
        bars +=
          '<text x="' + cx.toFixed(1) + '" y="' + (H - 12) + '" text-anchor="middle" fill="' + axis +
          '" font-size="11">' + escapeHtml(label) + "</text>";
      }
    });
    const hitboxes = days.map((d, i) => {
      const x = pad.l + i * (innerW / n);
      return (
        '<rect class="hit" data-i="' + i + '" x="' + x.toFixed(1) + '" y="' + pad.t +
        '" width="' + (innerW / n).toFixed(1) + '" height="' + innerH +
        '" fill="transparent"/>'
      );
    }).join("");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.innerHTML = grid + bars + hitboxes;
    svg.onmousemove = (ev) => {
      if (!tip) return;
      const el = ev.target;
      const idx = el && el.getAttribute && el.getAttribute("data-i");
      if (idx == null) { tip.hidden = true; return; }
      const d = days[Number(idx)];
      if (!d) { tip.hidden = true; return; }
      const rows = SERIES.map((s) =>
        "<div>" + escapeHtml(s.label) + "　<b>" +
        escapeHtml(metric === "official" ? (fmtCny(d[s.key] || 0) || "¥ 0") : fmt(d[s.key] || 0, metric === "credit" ? 2 : 0)) +
        "</b></div>"
      ).join("");
      const extra =
        "<div>缓存命中　<b>" + escapeHtml(fmt(d.cache_hit_tokens || 0, 0)) +
        (d.cache_hit_rate != null ? "（" + pct(d.cache_hit_rate) + "）" : "") + "</b></div>" +
        (d.cache_hit_saved
          ? "<div>缓存命中省　<b>" + escapeHtml(fmtCny(d.cache_hit_saved)) + "</b></div>"
          : "") +
        "<div>调用　<b>" + escapeHtml(fmt(d.calls || 0, 0)) + "</b></div>";
      tip.innerHTML = "<b>" + escapeHtml(d.day) + "</b>" + rows + extra;
      tip.hidden = false;
      const rect = box.getBoundingClientRect();
      let left = ev.clientX - rect.left + 12;
      let top = ev.clientY - rect.top + 12;
      if (left + 200 > rect.width) left = ev.clientX - rect.left - 200;
      if (top + 160 > rect.height) top = ev.clientY - rect.top - 150;
      tip.style.left = left + "px";
      tip.style.top = top + "px";
    };
    svg.onmouseleave = () => { if (tip) tip.hidden = true; };
  }

  function fillSessions(payload) {
    const list = Array.isArray(payload) ? payload : (payload && payload.sessions) || [];
    state.sessions = list || [];
    if (payload && !Array.isArray(payload)) {
      state.sessionTotals = { usage: payload.usage_totals || {}, official: payload.official_totals || {} };
      state.sessionTimeline = payload.timeline || { by_day: [] };
      if (payload.credit_rules) state.creditRules = payload.credit_rules;
    }
    renderSessions();
    renderSessionChart();
  }

  function modelTags(models) {
    if (!models) return '<span class="na">—</span>';
    return models
      .split(",")
      .map((m) => m.trim())
      .filter(Boolean)
      .map((m) => '<span class="tag" title="' + escapeHtml(m) + '">' + escapeHtml(m) + "</span>")
      .join("");
  }

  function lastCallLabel(s) {
    const u = s.usage || {};
    if (u.last_call_at) {
      // "2026-11-12 17:47:29" → 11-12 17:47
      const m = String(u.last_call_at).match(/(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})/);
      if (m) return { short: m[2] + "-" + m[3] + " " + m[4] + ":" + m[5], full: u.last_call_at + "（北京时间）" };
      return { short: u.last_call_at, full: u.last_call_at + "（北京时间）" };
    }
    return { short: when(s.updated_at || s.last_activity_at), full: whenFull(s.updated_at || s.last_activity_at) };
  }

  function renderCallsPane(sid) {
    const info = state.sessionCalls[sid] || {};
    if (info.loading) return '<p class="calls-loading">正在读取每次调用的时间……</p>';
    if (info.error) return '<p class="calls-error">' + escapeHtml(info.error) + "</p>";
    const calls = info.calls || [];
    if (!calls.length) return '<p class="calls-error">这个会话没有读到带用量的调用记录。</p>';
    const rows = calls.map((c) => {
      const saved = c.cache_hit_saved != null && c.official_cny_status === "ok"
        ? '<span class="saved">' + escapeHtml(fmtCny(c.cache_hit_saved)) + "</span>"
        : na("—");
      return (
        "<tr>" +
        "<td title='" + escapeHtml((c.time_local || "") + " 北京时间") + "'>" + escapeHtml(c.time_local || "—") + periodTag(c.period) + "</td>" +
        "<td class='clip' title='" + escapeHtml(c.model || "") + "'>" + escapeHtml(c.model || "—") + "</td>" +
        '<td class="num">' + escapeHtml(fmt(c.credit || 0, 2)) + "</td>" +
        '<td class="num">' + escapeHtml(fmt(c.uncached_input || 0, 0)) + "</td>" +
        '<td class="num">' + cacheCell(c.cache_hit_tokens, c.cache_hit_rate) + "</td>" +
        '<td class="num">' + escapeHtml(fmt(c.completion_tokens || 0, 0)) + "</td>" +
        '<td class="num">' + officialCell(c) + "</td>" +
        '<td class="num">' + saved + "</td>" +
        "</tr>"
      );
    }).join("");
    const more = (info.total || 0) > calls.length
      ? '<tr class="more-row"><td colspan="8">已显示 ' + calls.length + " / " + info.total +
        ' 次。点击「再加载」看更早的调用。<button type="button" class="btn-ghost" data-more="' +
        escapeHtml(sid) + '">再加载</button></td></tr>'
      : "";
    return (
      '<div class="table-wrap"><table class="calls-table">' +
      "<thead><tr>" +
      "<th>调用时间（北京）</th><th>模型</th><th class='num'>积分</th>" +
      "<th class='num'>未命中输入</th><th class='num'>缓存命中</th><th class='num'>输出</th>" +
      "<th class='num'>官方价</th><th class='num'>缓存省下</th>" +
      "</tr></thead><tbody>" + rows + more + "</tbody></table></div>"
    );
  }

  async function loadSessionCalls(sid, more) {
    if (!sid) return;
    const cur = state.sessionCalls[sid] || { calls: [], total: 0, offset: 0 };
    if (cur.loading) return;
    if (!more && cur.calls && cur.calls.length) {
      renderSessions();
      return;
    }
    const offset = more ? (cur.calls || []).length : 0;
    state.sessionCalls[sid] = Object.assign({}, cur, { loading: true, error: "" });
    renderSessions();
    try {
      const obj = await get("/api/sessions/" + encodeURIComponent(sid) + "/calls?limit=200&offset=" + offset);
      if (!obj || obj.ok === false) {
        state.sessionCalls[sid] = Object.assign({}, cur, { loading: false, error: (obj && obj.error) || "读取失败" });
      } else {
        const prev = more ? (cur.calls || []) : [];
        state.sessionCalls[sid] = {
          loading: false,
          error: "",
          calls: prev.concat(obj.calls || []),
          total: obj.total || 0,
          offset: obj.offset || 0,
        };
      }
    } catch (e) {
      state.sessionCalls[sid] = Object.assign({}, cur, { loading: false, error: "读取失败" });
    }
    renderSessions();
  }

  function renderSessions() {
    const filterEl = $("session-filter");
    const q = ((filterEl && filterEl.value) || "").trim().toLowerCase();
    const rows = state.sessions.filter((s) => {
      if (!q) return true;
      const blob = [s.title, s.model, s.display_id, (s.usage && s.usage.user_id) || ""].join(" ").toLowerCase();
      return blob.indexOf(q) >= 0;
    });
    const tb = $("session-table").querySelector("tbody");
    const isFilter = !!q;
    if (!rows.length) {
      const emptyTitle = q ? "没有符合条件的会话" : "还没有会话";
      const emptyDesc = q ? "试试别的标题、模型或账号。" : "这个数据目录里还没有读到会话。";
      tb.innerHTML = emptyRow(9, emptyTitle, emptyDesc);
      $("session-page").textContent = "0 条";
      renderSessionSummary([], isFilter);
      return;
    }
    renderSessionSummary(rows, isFilter);
    tb.innerHTML = rows
      .map((s) => {
        const u = s.usage || {};
        const hitRate = u.cache_hit_rate != null
          ? u.cache_hit_rate
          : (u.prompt_tokens ? Math.min(u.cache_hit_tokens || 0, u.prompt_tokens) / u.prompt_tokens : null);
        const models = u.model_counts ? Object.keys(u.model_counts).join(", ") : s.model;
        const open = state.openSession === s.id;
        const whenL = lastCallLabel(s);
        const savedLine = (u.cache_hit_saved != null && (u.official_cny_status === "ok" || u.official_cny_status === "partial"))
          ? "缓存命中省 " + fmtCny(u.cache_hit_saved)
          : "";
        const peakLine = [
          u.official_cny_peak ? "高峰 " + fmtCny(u.official_cny_peak) : "",
          u.official_cny_offpeak ? "闲时 " + fmtCny(u.official_cny_offpeak) : "",
          u.official_cny_standard ? "标准 " + fmtCny(u.official_cny_standard) : "",
        ].filter(Boolean).join(" · ");
        const callsHtml = open ? renderCallsPane(s.id) : "";
        const first = u.first_call_at ? "首次 " + u.first_call_at : "";
        const detail =
          "<tr class='session-detail' data-for='" + escapeHtml(s.id) + "'" + (open ? "" : " hidden") + ">" +
          "<td colspan='9'><div class='detail-grid'>" +
          "<div><div class='k'>调用</div><div class='v'>" + escapeHtml(fmt(u.calls || 0, 0)) + "</div></div>" +
          "<div><div class='k'>积分</div><div class='v'>" + escapeHtml(fmt(u.credit || 0, 2)) + "</div></div>" +
          "<div><div class='k'>输入 token</div><div class='v'>" + escapeHtml(fmt(u.prompt_tokens || 0, 0)) + "</div></div>" +
          "<div><div class='k'>缓存命中</div><div class='v'>" + escapeHtml(fmt(u.cache_hit_tokens || 0, 0)) +
            (hitRate == null ? "" : "（" + escapeHtml(pct(hitRate)) + "）") + "</div></div>" +
          "<div><div class='k'>未命中输入</div><div class='v'>" + escapeHtml(fmt(u.uncached_input || 0, 0)) + "</div></div>" +
          "<div><div class='k'>输出 token</div><div class='v'>" + escapeHtml(fmt(u.completion_tokens || 0, 0)) + "</div></div>" +
          "<div><div class='k'>官方价</div><div class='v'>" + officialCell(u) + "</div></div>" +
          "<div><div class='k'>缓存命中省</div><div class='v'>" + (savedLine ? '<span class="saved">' + escapeHtml(fmtCny(u.cache_hit_saved)) + "</span>" : na("没读到")) + "</div></div>" +
          "<div><div class='k'>首次调用</div><div class='v'>" + escapeHtml(u.first_call_at || "—") + "</div></div>" +
          "<div><div class='k'>最近调用</div><div class='v'>" + escapeHtml(u.last_call_at || "—") + "</div></div>" +
          "</div>" +
          (peakLine ? "<p class='footnote tight'>" + escapeHtml(peakLine) + "</p>" : "") +
          "<p class='footnote'>不含对话正文。时间是北京时间。模型：" + escapeHtml(models || "—") +
          (u.official_cny_note ? "。" + escapeHtml(u.official_cny_note) : "") +
          (u.wb_rule_expected_note ? " " + escapeHtml(u.wb_rule_expected_note) : "") +
          (first ? "。" + escapeHtml(first) : "") + "</p>" +
          callsHtml +
          "</td></tr>";
        return (
          "<tr class='session-row" + (open ? " is-open" : "") + "' data-id='" + escapeHtml(s.id) + "'>" +
          "<td class='clip title' title='" + escapeHtml(s.title || "（无标题）") + "'>" + escapeHtml(s.title || "（无标题）") + "</td>" +
          "<td class='model'>" + modelTags(models) + "</td>" +
          "<td class='clip' title='" + escapeHtml(s.display_id || "—") + "'>" + escapeHtml(s.display_id || "—") + "</td>" +
          '<td class="num">' + fmt(u.calls || 0, 0) + "</td>" +
          '<td class="num">' + fmt(u.credit || 0, 2) + "</td>" +
          '<td class="num" title="输入 ' + fmt(u.prompt_tokens || 0, 0) + " / 输出 " + fmt(u.completion_tokens || 0, 0) + '">' +
            fmt(u.total_tokens || ((u.prompt_tokens || 0) + (u.completion_tokens || 0)), 0) + "</td>" +
          '<td class="num cache">' + cacheCell(u.cache_hit_tokens, hitRate) + "</td>" +
          '<td class="num">' + officialCell(u) + "</td>" +
          "<td class='date' title='" + escapeHtml(whenL.full) + "'>" + escapeHtml(whenL.short) + "</td>" +
          "</tr>" +
          detail
        );
      })
      .join("");
    const totalAll = (state.sessionTotals && state.sessionTotals.usage && state.sessionTotals.usage.session_count) || "";
    $("session-page").textContent =
      "本页 " + rows.length + " 条（最多 200 条最近会话）" +
      (totalAll && !isFilter ? "，合计覆盖全部 " + totalAll + " 个会话" : "") +
      (isFilter ? "，当前为筛选结果" : "");
    tb.querySelectorAll("tr.session-row").forEach((tr) => {
      tr.addEventListener("click", () => {
        const id = tr.getAttribute("data-id");
        const opening = state.openSession !== id;
        state.openSession = opening ? id : "";
        renderSessions();
        if (opening) loadSessionCalls(id, false);
      });
    });
    tb.querySelectorAll("button[data-more]").forEach((btn) => {
      btn.addEventListener("click", (ev) => {
        ev.stopPropagation();
        loadSessionCalls(btn.getAttribute("data-more"), true);
      });
    });
  }

  function fillCreditRules(rules, rates) {
    const body = $("wb-rules-body");
    const formula = $("wb-rules-formula");
    const tb = $("wb-multiplier-table") && $("wb-multiplier-table").querySelector("tbody");
    const r = rules || state.creditRules || (rates && rates.workbuddy_credit_rules) || {};
    const mul = (r.multipliers || (rates && rates.official_multipliers) || {});
    if (body) {
      const summary = r.summary || [];
      const src = r.source || mul.source || "";
      const title = r.source_title || mul.source_title || "WorkBuddy 官方文档";
      const at = r.retrieved_at || mul.retrieved_at || "";
      const srcHtml = src
        ? '<p class="rules-meta">来源：<a href="' + escapeHtml(src) + '" target="_blank" rel="noopener noreferrer">' +
          escapeHtml(title) + "</a>" + (at ? "（取自 " + escapeHtml(at) + "）" : "") + "</p>"
        : '<p class="rules-meta">没读到官方规则来源。</p>';
      const list = summary.length
        ? "<ul class='rules-list'>" + summary.map((s) => "<li>" + escapeHtml(s) + "</li>").join("") + "</ul>"
        : "<p class='footnote'>没读到官方规则正文。</p>";
      body.innerHTML = srcHtml + list;
    }
    if (formula) {
      const st = r.exact_formula_status || "没读到";
      const note = r.exact_formula_note || "官方未公布每百万 token 积分费率，无法按规则算出应扣积分。";
      formula.textContent = "精确公式：" + (st === "没读到" ? "没读到。" : st + "。") + " " + note;
    }
    if (tb) {
      const models = (mul.models) || {};
      const keys = Object.keys(models);
      if (!keys.length) {
        tb.innerHTML = emptyRow(3, "还没有倍率", "官方「选择模型」页的倍率表还没读到。");
      } else {
        tb.innerHTML = keys.map((name) => {
          const v = models[name];
          const miss = v === "没读到" || v === "" || v == null;
          const shown = miss ? na("没读到") : (v === 0 || v === 0.0 ? "限时免费（x0）" : "x" + v);
          return "<tr><td>" + escapeHtml(name) + '</td><td class="num">' +
            (miss ? shown : escapeHtml(String(shown))) + "</td><td class='note'>" +
            (miss ? "官方未标倍率" : "数字越低消耗越少") + "</td></tr>";
        }).join("");
      }
    }
  }

  /* ---------- 加载 ---------- */
  async function reload() {
    const first = !state.usage;
    if (first) document.body.setAttribute("aria-busy", "true");
    try {
      const meta = await get("/api/meta" + qed());
      fillMeta(meta);
      const [usage, sessions, accounts] = await Promise.all([
        get("/api/usage" + qed()),
        get("/api/sessions" + qed() + (qed() ? "&" : "?") + "limit=200"),
        get("/api/accounts" + qed()),
      ]);
      state.usage = usage;
      fillRates(usage);
      fillStats(usage);
      fillInfer(usage);
      fillModels(usage);
      fillPrices(usage);
      fillSessions(sessions || {});
      fillCreditRules((sessions && sessions.credit_rules) || (usage.rates || {}).workbuddy_credit_rules, usage.rates || {});
      fillAccounts(accounts);
      await fillPreflight();

      const ed = meta.edition || {};
      if (ed.dual && ed.edition !== "intl") {
        try {
          state.intlUsage = await get("/api/usage" + qed({ edition: "intl" }));
        } catch (e) {
          state.intlUsage = null;
        }
      } else {
        state.intlUsage = null;
      }
      renderTokenPage();
    } catch (e) {
      $("token-updated").textContent = "读取失败：" + e.message;
      $("client-status").textContent = "读取失败";
    } finally {
      document.body.removeAttribute("aria-busy");
    }
  }

  /* ---------- 事件 ---------- */
  $("reload").addEventListener("click", reload);
  $("reload-tokens").addEventListener("click", reload);
  $("conversion").addEventListener("change", () => {
    state.conversion = $("conversion").value;
    reload();
  });
  $("session-filter").addEventListener("input", renderSessions);
  ["sync-yes", "sync-confirm", "sync-token", "sync-source", "sync-target"].forEach((id) => {
    $(id).addEventListener("input", syncReady);
    $(id).addEventListener("change", syncReady);
  });
  $("sync-run").addEventListener("click", doSync);
  $("sync-refresh").addEventListener("click", fillPreflight);

  function bindPills(id, field, after) {
    const box = $(id);
    box.querySelectorAll("button[data-range]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state[field] = btn.getAttribute("data-range");
        box.querySelectorAll("button").forEach((b) => b.classList.toggle("is-on", b === btn));
        after();
      });
    });
  }
  bindPills("overview-range", "overviewRange", renderTokenPage);
  bindPills("chart-range", "chartRange", renderTokenPage);
  bindPills("session-chart-range", "sessionChartRange", renderSessionChart);
  const metricBox = $("session-chart-metric");
  if (metricBox) {
    metricBox.querySelectorAll("button[data-metric]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.sessionChartMetric = btn.getAttribute("data-metric");
        metricBox.querySelectorAll("button").forEach((b) => b.classList.toggle("is-on", b === btn));
        renderSessionChart();
      });
    });
  }
  window.addEventListener("resize", () => {
    if (state.page === "tokens") renderTokenPage();
    if (state.page === "sessions") renderSessionChart();
  });

  document.querySelectorAll("[data-theme-set]").forEach((btn) => {
    btn.addEventListener("click", () => setTheme(btn.getAttribute("data-theme-set")));
  });
  try {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = () => {
      if (!document.documentElement.getAttribute("data-theme")) {
        syncThemeButtons();
        if (state.usage) renderTokenPage();
        if (state.page === "sessions") renderSessionChart();
      }
    };
    if (mq.addEventListener) mq.addEventListener("change", onChange);
    else if (mq.addListener) mq.addListener(onChange);
  } catch (e) {}
  syncThemeButtons();

  showPage(currentHashPage(), { silent: true });
  reload();
})();
