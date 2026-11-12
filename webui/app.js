(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PAGES = ["accounts", "tokens", "credits", "sessions"];
  const SERIES = [
    { key: "cache_hit_tokens", label: "缓存命中", color: "#22c55e", cls: "cache" },
    { key: "uncached_input", label: "新输入", color: "#2dd4bf", cls: "input" },
    { key: "completion_tokens", label: "输出", color: "#8b7cf6", cls: "output" },
  ];

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
    if (n === null || n === undefined) return "没读到";
    return (Number(n) * 100).toFixed(1) + "%";
  }
  function when(ms) {
    if (!ms) return "—";
    const n = Number(ms);
    const sec = n > 10_000_000_000 ? n / 1000 : n;
    const dt = new Date(sec * 1000);
    if (isNaN(dt.getTime())) return "—";
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
    if (range === "month") return day.slice(0, 7) === today.slice(0, 7);
    return true; // all
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
      grid.innerHTML = '<p class="empty">这个数据目录里还没有读到账号。</p>';
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
      ["已消耗积分", fmt(t.credit, 2), "spark"],
      ["套餐折算", t.estimated_cny == null ? "没读到" : "¥ " + fmt(t.estimated_cny, 2), "yen"],
      ["API 等效", t.equivalent_cny == null ? "没读到" : "¥ " + fmt(t.equivalent_cny, 2), "trend"],
      ["缓存命中率", pct(t.cache_hit_rate), "cache"],
    ];
    $("stats").innerHTML = items
      .map(([l, n]) => '<div class="metric"><div class="k">' + escapeHtml(l) + '</div><div class="v">' + escapeHtml(n) + "</div></div>")
      .join("");
  }

  function fillInfer(usage) {
    const tb = $("infer-table").querySelector("tbody");
    const rows = usage.by_model || [];
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="7" class="empty">这个数据目录里还没有读到 rawUsage。</td></tr>';
      return;
    }
    tb.innerHTML = rows
      .map((r) => {
        const inf = r.inference || {};
        const ok = inf.status === "ok" && inf.tokens_per_credit;
        const tpc = ok ? inf.tokens_per_credit : null;
        const cell = (k) => (!tpc || tpc[k] == null ? "没读到" : fmt(tpc[k], 0));
        const status = inf.status === "ok" ? "已反推" : "没读到";
        return (
          "<tr>" +
          "<td>" + escapeHtml(r.model) + "</td>" +
          '<td class="num">' + fmt(inf.calls != null ? inf.calls : r.calls, 0) + "</td>" +
          "<td>" + escapeHtml(status) + "</td>" +
          '<td class="num">' + escapeHtml(cell("uncached_input")) + "</td>" +
          '<td class="num">' + escapeHtml(cell("cache_hit")) + "</td>" +
          '<td class="num">' + escapeHtml(cell("output")) + "</td>" +
          "<td>" + escapeHtml(inf.reason || (ok ? "三项分开计算，未使用默认汇率。" : "")) + "</td>" +
          "</tr>"
        );
      })
      .join("");
  }

  function fillModels(usage) {
    const tb = $("model-table").querySelector("tbody");
    const rows = usage.by_model || [];
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="14" class="empty">这个数据目录里还没有读到 rawUsage。</td></tr>';
      return;
    }
    tb.innerHTML = rows
      .map((r) => {
        const mul =
          r.official_multiplier_status === "没读到" || r.official_multiplier == null
            ? "没读到"
            : "x" + r.official_multiplier;
        const cny =
          r.estimated_cny_status === "没读到" || r.estimated_cny == null
            ? "没读到"
            : "¥ " + fmt(r.estimated_cny, 2);
        const inf = r.inference || {};
        const tpc = (inf.status === "ok" && inf.tokens_per_credit) ? inf.tokens_per_credit : null;
        const cellInf = (k) => {
          if (!tpc || tpc[k] == null) return "没读到";
          return fmt(tpc[k], 0);
        };
        const eq = (r.equivalent_cny_status === "没读到" || r.equivalent_cny == null)
          ? "没读到" : "¥ " + fmt(r.equivalent_cny, 2);
        return (
          "<tr>" +
          "<td>" + escapeHtml(r.model) + "</td>" +
          '<td class="num">' + fmt(r.calls, 0) + "</td>" +
          '<td class="num">' + fmt(r.prompt_tokens, 0) + "</td>" +
          '<td class="num">' + fmt(r.completion_tokens, 0) + "</td>" +
          '<td class="num">' + fmt(r.thinking_tokens, 0) + "</td>" +
          '<td class="num">' + pct(r.cache_hit_rate) + "</td>" +
          '<td class="num">' + (r.tokens_per_credit_uncached == null ? "没读到" : fmt(r.tokens_per_credit_uncached, 0)) + "</td>" +
          '<td class="num">' + fmt(r.credit, 2) + "</td>" +
          '<td class="num">' + escapeHtml(cny) + "</td>" +
          '<td class="num">' + escapeHtml(eq) + "</td>" +
          '<td class="num">' + escapeHtml(cellInf("uncached_input")) + "</td>" +
          '<td class="num">' + escapeHtml(cellInf("cache_hit")) + "</td>" +
          '<td class="num">' + escapeHtml(cellInf("output")) + "</td>" +
          "<td>" + escapeHtml(mul) + "</td>" +
          "</tr>"
        );
      })
      .join("");
    const cc = usage.cross_check || {};
    $("cross-check").textContent = cc.summary || "";
  }

  function fillPrices(usage) {
    const rates = (usage && usage.rates) || {};
    const prices = (rates.api_unit_prices || {}).models || [];
    const tb = document.querySelector("#price-table tbody");
    if (tb) {
      if (!prices.length) {
        tb.innerHTML = '<tr><td colspan="6" class="empty">价格表里还没有按量单价。</td></tr>';
      } else {
        tb.innerHTML = prices.map((e) => {
          const miss = e.status === "没读到";
          const cell = (k) => (miss || e[k] == null || e[k] === "没读到") ? "没读到" : fmt(e[k], 4);
          return "<tr><td>" + escapeHtml(e.id || "") + "</td>" +
            '<td class="num">' + cell("input") + "</td>" +
            '<td class="num">' + cell("output") + "</td>" +
            '<td class="num">' + cell("cached_read") + "</td>" +
            "<td>" + escapeHtml(e.source_via || e.source || "") + "</td>" +
            "<td>" + escapeHtml(e.retrieved_at || "") + "</td></tr>";
        }).join("");
      }
    }
    const afp = rates.volc_afp || {};
    const atb = document.querySelector("#afp-table tbody");
    const models = afp.models || {};
    const names = Object.keys(models);
    if (atb) {
      if (!names.length) {
        atb.innerHTML = '<tr><td colspan="4" class="empty">没有已核对的 AFP 系数。</td></tr>';
      } else {
        atb.innerHTML = names.map((n) => {
          const m = models[n] || {};
          const miss = m.status === "没读到";
          const note = (m.limited_offer ? "限时折扣 " : "") + (m.note || "");
          return "<tr><td>" + escapeHtml(n) + "</td>" +
            '<td class="num">' + (miss || m.input_coef == null ? "没读到" : m.input_coef) + "</td>" +
            '<td class="num">' + (miss || m.output_coef == null ? "没读到" : m.output_coef) + "</td>" +
            "<td>" + escapeHtml(note) + "</td></tr>";
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

    if (bundle.status === "没读到" && state.tokenSource === "codebuddy") {
      $("comp-bar").innerHTML = "";
      $("comp-legend").innerHTML = "";
      $("token-metrics").innerHTML =
        '<div class="metric span-all"><div class="k">CodeBuddy CLI</div>' +
        '<div class="v msg">' +
        escapeHtml(bundle.reason || "没读到") + "</div></div>";
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
        const share = sum ? ((p.value / sum) * 100).toFixed(1) + "%" : "没读到";
        return '<span><i class="swatch swatch-' + p.cls + '"></i>' +
          escapeHtml(p.label) + " " + share + "</span>";
      })
      .join("");

    const inputTok = totals.prompt_tokens != null ? totals.prompt_tokens : (totals.uncached_input || 0) + (totals.cache_hit_tokens || 0);
    const totalTok = totals.total_tokens || (inputTok + (totals.completion_tokens || 0));
    const hit = totals.cache_hit_rate != null
      ? totals.cache_hit_rate
      : (inputTok ? Math.min(totals.cache_hit_tokens || 0, inputTok) / inputTok : null);
    const metrics = [
      ["总 Token", compact(totalTok)],
      ["输入 Token", compact(inputTok)],
      ["输出 Token", compact(totals.completion_tokens)],
      ["缓存命中率", pct(hit)],
    ];
    $("token-metrics").innerHTML = metrics
      .map(([l, n]) => '<div class="metric"><div class="k">' + escapeHtml(l) + '</div><div class="v">' + escapeHtml(n) + "</div></div>")
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
    legend.innerHTML =
      SERIES.map((s) => '<span><i class="swatch swatch-' + s.cls + '"></i>' + escapeHtml(s.label) + "</span>").join("") +
      '<span><i class="swatch swatch-calls"></i>调用次数</span>';

    if (!days || !days.length) {
      svg.innerHTML = "";
      empty.hidden = false;
      empty.textContent = "没读到该时间段的按日用量。";
      tip.hidden = true;
      return;
    }
    empty.hidden = true;

    const box = $("chart-box");
    const W = Math.max(box.clientWidth || 720, 480);
    const H = Math.max(box.clientHeight || 300, 240);
    const pad = { l: 52, r: 48, t: 10, b: 36 };
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
        '" stroke="#eee" stroke-width="1"/>' +
        '<text x="' + (pad.l - 8) + '" y="' + (y + 4) + '" text-anchor="end" fill="#a1a1aa" font-size="11">' +
        escapeHtml(compact(v)) + "</text>";
    }
    for (let i = 0; i <= ticks; i++) {
      const v = maxCalls * (i / ticks);
      const y = yCall(v);
      grid +=
        '<text x="' + (W - pad.r + 8) + '" y="' + (y + 4) + '" text-anchor="start" fill="#a1a1aa" font-size="11">' +
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
        const rx = top ? Math.min(4, bw / 2) : 0;
        bars +=
          '<rect x="' + x.toFixed(1) + '" y="' + y.toFixed(1) + '" width="' + bw.toFixed(1) +
          '" height="' + Math.max(seg.h, 0.5).toFixed(1) + '" fill="' + seg.s.color +
          '" rx="' + rx + '" ry="' + rx + '" data-i="' + i + '"/>';
        if (top && rx) {
          // 只圆顶部：用一个不圆的矩形盖住下半圆角
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
          '<text x="' + cx.toFixed(1) + '" y="' + (H - 12) + '" text-anchor="middle" fill="#a1a1aa" font-size="11">' +
          escapeHtml(label) + "</text>";
      }
    });

    const line =
      '<polyline fill="none" stroke="#64748b" stroke-width="1.6" stroke-dasharray="4 3" points="' +
      pts.join(" ") + '"/>' +
      pts
        .map((p) => {
          const [cx, cy] = p.split(",");
          return '<circle cx="' + cx + '" cy="' + cy + '" r="2.2" fill="#fff" stroke="#64748b" stroke-width="1.4"/>';
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
  function fillSessions(list) {
    state.sessions = list || [];
    renderSessions();
  }

  function renderSessions() {
    const q = ($("session-filter").value || "").trim().toLowerCase();
    const rows = state.sessions.filter((s) => {
      if (!q) return true;
      const blob = [s.title, s.model, s.display_id, (s.usage && s.usage.user_id) || ""].join(" ").toLowerCase();
      return blob.indexOf(q) >= 0;
    });
    const tb = $("session-table").querySelector("tbody");
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="8" class="empty">没有符合条件的会话。</td></tr>';
      $("session-page").textContent = "0 条";
      return;
    }
    tb.innerHTML = rows
      .map((s) => {
        const u = s.usage || {};
        const hit = u.prompt_tokens ? pct(u.cache_hit_tokens / u.prompt_tokens) : "—";
        const models = u.model_counts ? Object.keys(u.model_counts).join(", ") : s.model;
        const open = state.openSession === s.id;
        const detail =
          "<tr class='session-detail' data-for='" + escapeHtml(s.id) + "'" + (open ? "" : " hidden") + ">" +
          "<td colspan='8'><div class='detail-grid'>" +
          "<div><div class='k'>调用</div><div class='v'>" + escapeHtml(fmt(u.calls || 0, 0)) + "</div></div>" +
          "<div><div class='k'>积分</div><div class='v'>" + escapeHtml(fmt(u.credit || 0, 2)) + "</div></div>" +
          "<div><div class='k'>输入 token</div><div class='v'>" + escapeHtml(fmt(u.prompt_tokens || 0, 0)) + "</div></div>" +
          "<div><div class='k'>输出 token</div><div class='v'>" + escapeHtml(fmt(u.completion_tokens || 0, 0)) + "</div></div>" +
          "<div><div class='k'>缓存命中</div><div class='v'>" + escapeHtml(fmt(u.cache_hit_tokens || 0, 0)) + "</div></div>" +
          "<div><div class='k'>未命中输入</div><div class='v'>" + escapeHtml(fmt(u.uncached_input || 0, 0)) + "</div></div>" +
          "<div><div class='k'>思考 token</div><div class='v'>" + escapeHtml(fmt(u.thinking_tokens || 0, 0)) + "</div></div>" +
          "<div><div class='k'>账号</div><div class='v'>" + escapeHtml(s.display_id || "—") + "</div></div>" +
          "</div>" +
          "<p class='footnote'>不含对话正文。模型：" + escapeHtml(models || "—") + "</p>" +
          "</td></tr>";
        return (
          "<tr class='session-row" + (open ? " is-open" : "") + "' data-id='" + escapeHtml(s.id) + "'>" +
          "<td>" + escapeHtml(s.title || "（无标题）") + "</td>" +
          "<td>" + escapeHtml(models || "—") + "</td>" +
          "<td>" + escapeHtml(s.display_id || "—") + "</td>" +
          '<td class="num">' + fmt(u.calls || 0, 0) + "</td>" +
          '<td class="num">' + fmt(u.credit || 0, 2) + "</td>" +
          '<td class="num">' + fmt(u.prompt_tokens || 0, 0) + " / " + fmt(u.completion_tokens || 0, 0) + "</td>" +
          '<td class="num">' + escapeHtml(hit) + "</td>" +
          "<td>" + escapeHtml(when(s.updated_at || s.last_activity_at)) + "</td>" +
          "</tr>" +
          detail
        );
      })
      .join("");
    $("session-page").textContent = "本页 " + rows.length + " 条（最多 200 条最近会话）";
    tb.querySelectorAll("tr.session-row").forEach((tr) => {
      tr.addEventListener("click", () => {
        const id = tr.getAttribute("data-id");
        state.openSession = state.openSession === id ? "" : id;
        renderSessions();
      });
    });
  }

  /* ---------- 加载 ---------- */
  async function reload() {
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
      fillSessions(sessions.sessions || []);
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
  window.addEventListener("resize", () => {
    if (state.page === "tokens") renderTokenPage();
  });

  showPage(currentHashPage(), { silent: true });
  reload();
})();
