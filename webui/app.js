(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = {
    meta: null,
    usage: null,
    sessions: [],
    accounts: [],
    conversion: "",
    edition: "",
  };

  function fmt(n, d) {
    if (n === null || n === undefined || n === "") return "—";
    const x = Number(n);
    if (!isFinite(x)) return "—";
    return x.toLocaleString("zh-CN", { maximumFractionDigits: d == null ? 2 : d });
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

  async function get(path) {
    const res = await fetch(path, { credentials: "same-origin" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.message || data.error || ("请求失败 " + res.status));
    }
    return data;
  }

  function fillMeta(meta) {
    state.meta = meta;
    const ed = meta.edition || {};
    const parts = [];
    parts.push((ed.label || "") + "  " + (ed.path || ""));
    if (ed.dual) parts.push("本机同时有国内版和国际版，可用下方切换。");
    if (ed.notice) parts.push(ed.notice);
    $("top-meta").textContent = parts.join("\n");
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
            "<button type='button' class='ghost' data-ed='" +
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
    }
  }

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
      ["模型调用", fmt(t.calls, 0)],
      ["实际积分", fmt(t.credit, 2)],
      ["估算费用（元）", t.estimated_cny == null ? "没读到" : "¥ " + fmt(t.estimated_cny, 2)],
      ["缓存命中率", pct(t.cache_hit_rate)],
      ["去缓存效率", t.tokens_per_credit_uncached == null ? "没读到" : fmt(t.tokens_per_credit_uncached, 0) + " token/积分"],
      ["会话文件", fmt(t.files_scanned, 0)],
    ];
    $("stats").innerHTML = items
      .map(([l, n]) => '<div class="stat"><div class="n">' + escapeHtml(n) + '</div><div class="l">' + escapeHtml(l) + "</div></div>")
      .join("");
  }

  function fillModels(usage) {
    const tb = $("model-table").querySelector("tbody");
    const rows = usage.by_model || [];
    if (!rows.length) {
      tb.innerHTML = '<tr><td colspan="11" class="empty">这个数据目录里还没有读到 rawUsage。</td></tr>';
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
        const unc = r.tokens_per_credit_uncached == null ? "没读到" : fmt(r.tokens_per_credit_uncached, 0);
        const tot = r.tokens_per_credit_total == null ? "没读到" : fmt(r.tokens_per_credit_total, 0);
        return (
          "<tr>" +
          "<td>" + escapeHtml(r.model) + "</td>" +
          '<td class="num">' + fmt(r.calls, 0) + "</td>" +
          '<td class="num">' + fmt(r.prompt_tokens, 0) + "</td>" +
          '<td class="num">' + fmt(r.completion_tokens, 0) + "</td>" +
          '<td class="num">' + fmt(r.thinking_tokens, 0) + "</td>" +
          '<td class="num">' + pct(r.cache_hit_rate) + "</td>" +
          '<td class="num">' + escapeHtml(unc) + "</td>" +
          '<td class="num">' + escapeHtml(tot) + "</td>" +
          '<td class="num">' + fmt(r.credit, 2) + "</td>" +
          '<td class="num">' + escapeHtml(cny) + "</td>" +
          "<td>" + escapeHtml(mul) + "</td>" +
          "</tr>"
        );
      })
      .join("");
    const cc = usage.cross_check || {};
    $("cross-check").textContent = cc.summary || "";
  }

  function fillCodebuddy(cb) {
    const el = $("codebuddy-body");
    if (!cb) {
      el.textContent = "没读到";
      return;
    }
    if (cb.status === "没读到") {
      el.innerHTML = "<p>" + escapeHtml(cb.reason || "没读到 CodeBuddy CLI 用量。") + "</p>";
      (cb.reports || []).forEach((r) => {
        el.innerHTML += "<p class='footnote'>" + escapeHtml((r.path || "") + "：" + (r.reason || r.status)) + "</p>";
      });
      return;
    }
    const t = cb.totals || {};
    let html =
      "<p>已从本机 CodeBuddy 目录读到用量：调用 " +
      fmt(t.calls, 0) + "，积分 " + fmt(t.credit, 2) +
      (t.estimated_cny == null ? "" : "，估算 ¥ " + fmt(t.estimated_cny, 2)) +
      "。</p>";
    (cb.reports || []).forEach((r) => {
      html += "<p class='footnote'>" + escapeHtml(r.path) + " · 文件 " + fmt(r.files_scanned, 0) + " · 调用 " + fmt(r.calls, 0) + "</p>";
      if (r.by_model && r.by_model.length) {
        html +=
          "<div class='table-wrap'><table><thead><tr><th>模型</th><th class='num'>调用</th><th class='num'>积分</th><th class='num'>缓存命中率</th></tr></thead><tbody>" +
          r.by_model
            .map(
              (m) =>
                "<tr><td>" +
                escapeHtml(m.model) +
                "</td><td class='num'>" +
                fmt(m.calls, 0) +
                "</td><td class='num'>" +
                fmt(m.credit, 2) +
                "</td><td class='num'>" +
                pct(m.cache_hit_rate) +
                "</td></tr>"
            )
            .join("") +
          "</tbody></table></div>";
      }
    });
    el.innerHTML = html;
  }

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
      tb.innerHTML = '<tr><td colspan="7" class="empty">没有符合条件的会话。</td></tr>';
      $("session-page").textContent = "0 条";
      return;
    }
    tb.innerHTML = rows
      .map((s) => {
        const u = s.usage || {};
        const hit =
          u.prompt_tokens ? pct(u.cache_hit_tokens / u.prompt_tokens) : "—";
        const models = u.model_counts ? Object.keys(u.model_counts).join(", ") : s.model;
        return (
          "<tr>" +
          "<td>" + escapeHtml(s.title || "（无标题）") + "</td>" +
          "<td>" + escapeHtml(models || "—") + "</td>" +
          '<td class="num">' + fmt(u.calls || 0, 0) + "</td>" +
          '<td class="num">' + fmt(u.credit || 0, 2) + "</td>" +
          '<td class="num">' + fmt(u.prompt_tokens || 0, 0) + " / " + fmt(u.completion_tokens || 0, 0) + "</td>" +
          '<td class="num">' + escapeHtml(hit) + "</td>" +
          "<td>" + escapeHtml(when(s.updated_at || s.last_activity_at)) + "</td>" +
          "</tr>"
        );
      })
      .join("");
    $("session-page").textContent = "本页 " + rows.length + " 条（最多 200 条最近会话）";
  }

  function fillAccounts(data) {
    state.accounts = data.accounts || [];
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
  }

  async function fillPreflight() {
    const el = $("sync-preflight");
    try {
      const pf = await get("/api/sync/preflight" + qed());
      el.className = "preflight " + (pf.clients_running ? "bad" : pf.ok ? "ok" : "bad");
      el.textContent = pf.message || pf.error || "";
      if (pf.clients && pf.clients.length) {
        el.textContent += " 进程：" + pf.clients.map((c) => c.name).join("、") + "。";
      }
    } catch (e) {
      el.className = "preflight bad";
      el.textContent = "预检失败：" + e.message;
    }
    syncReady();
  }

  function qed() {
    const p = new URLSearchParams();
    if (state.edition) p.set("edition", state.edition);
    if (state.conversion) p.set("conversion", state.conversion);
    const s = p.toString();
    return s ? "?" + s : "";
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
      fillModels(usage);
      fillCodebuddy(usage.codebuddy);
      fillSessions(sessions.sessions || []);
      fillAccounts(accounts);
      await fillPreflight();
    } catch (e) {
      $("top-meta").textContent = "读取失败：" + e.message;
    }
  }

  $("reload").addEventListener("click", reload);
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

  reload();
})();
