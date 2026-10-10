// EUServ Telegram 重启指令转发器
// Telegram webhook → 校验 secret_token → 触发 GitHub Actions (webreboot.yml)
// 授权校验在 Actions 里完成（chat_id 对比 TG_USER_ID secret），Worker 只做转发。

const REPO = "wimdaw/EUServ_Renewal";
const WORKFLOW = "webreboot.yml";

function parseCommand(text) {
  // /reboot [目标] [理由]  目标: 1/2=账号序号, 合同号(如 450559), all(默认)
  // 理由: 1=Maintenance 2=Server does not respond(默认) 3=Other
  // /reinstall [合同号] [os_id|list] [confirm]
  const rm = text.match(/^\/(reboot|webreboot)(@\S+)?(\s+(\S+))?(\s+(\S+))?/i);
  if (rm) {
    const target = (rm[4] || "all").toLowerCase();
    const reason = ["1", "2", "3"].includes(rm[6]) ? rm[6] : "2";
    return { mode: "reboot", inputs: { mode: "reboot", target, reason } };
  }
  const ri = text.match(/^\/reinstall(@\S+)?(\s+(\S+))?(\s+(\S+))?(\s+(\S+))?/i);
  if (ri) {
    const target = ri[3] || "";
    const os = ri[5] || "list";
    const confirm = (ri[7] || "").toLowerCase() === "confirm" ? "true" : "false";
    return {
      mode: "reinstall",
      inputs: { mode: "reinstall", target, os, confirm, os_lang: "en" },
    };
  }
  return null;
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("EUServ TG Reboot webhook is running.\n");
    }
    const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
    if (!env.TG_WEBHOOK_SECRET || secret !== env.TG_WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }

    const msg = update && update.message;
    const text = msg && typeof msg.text === "string" ? msg.text.trim() : "";
    const cmd = text ? parseCommand(text) : null;
    if (!cmd) return new Response("ok"); // 非重启指令直接忽略

    const chatId = String(msg.chat.id);
    let dispatchStatus = 0;
    try {
      const resp = await fetch(
        `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
        {
          method: "POST",
          headers: {
            Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
            Accept: "application/vnd.github+json",
            "User-Agent": "euserv-tg-reboot-worker",
            "X-GitHub-Api-Version": "2022-11-28",
          },
          body: JSON.stringify({
            ref: "main",
            inputs: { chat_id: chatId, ...cmd.inputs },
          }),
        }
      );
      dispatchStatus = resp.status;
      if (!resp.ok) {
        console.error("dispatch failed:", resp.status, await resp.text());
      }
    } catch (e) {
      console.error("dispatch error:", e && e.message);
    }

    // Telegram 只要求快速 200；执行结果由 Actions 机器人私聊回报
    return new Response(JSON.stringify({ ok: true, dispatch: dispatchStatus }), {
      headers: { "content-type": "application/json" },
    });
  },
};
