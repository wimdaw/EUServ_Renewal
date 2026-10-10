# SPDX-License-Identifier: GPL-3.0-or-later
"""EUServ 面板电源管理 (power on/off)。

用法:
  python Euserv_Power.py probe [目标]     # 抓电源页 dump + 解析状态（只读）
  python Euserv_Power.py on [目标]        # 若关机则执行开机（幂等）

目标: 账号序号(1/2)、合同号(如 450559)或 all，默认 all

背景: 容器内 reboot -f 后 EUServ 宿主不会自动拉起容器（面板显示关机），
Webreboot 的 reset 对已停止容器只是排队，真正开机要走 Power on/off 页。

面板接口（逆向）:
  1. POST index.iphp subaction=choose_order & ord_no=<合同号>   # 选中合同
  2. GET  index.iphp?sess_id=..&subaction=show_kc2_customer_server_start_stop
     页面含电源状态文本与 Power on/off 动作（JS 变量构造 GET）
"""

import re
import sys

from Euserv_Renewal import LogLevel, RenewalBot, parse_accounts
from Euserv_WebReboot import _choose_order, _parse_ajax_var
import Euserv_Renewal as er


def _power_page(bot: RenewalBot, order_id: str) -> str:
    """选中合同并抓取 Power on/off 页面原文。"""
    _choose_order(bot, order_id)
    resp = bot.session.get(
        f"{er.EUSERV_BASE_URL}"
        f"?sess_id={bot.sess_id}"
        f"&subaction=show_kc2_customer_server_start_stop",
        headers={"user-agent": er.USER_AGENT},
        timeout=er.HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    with open(f"power_{order_id}.html", "w", encoding="utf-8") as f:
        f.write(resp.text)
    return resp.text


def _status_of(html: str) -> str:
    t = re.sub(r"<[^>]+>", " ", html)
    t = re.sub(r"\s+", " ", t).lower()
    for kw, st in [
        ("is running", "running"), ("is stopped", "stopped"),
        ("powered on", "running"), ("powered off", "stopped"),
        ("läuft", "running"), ("gestoppt", "stopped"),
        ("running", "running"), ("stopped", "stopped"),
        ("ausgeschaltet", "stopped"), ("eingeschaltet", "running"),
    ]:
        if kw in t:
            return st
    return "unknown"


def _extract_js_var(page: str, name: str) -> str | None:
    m = re.search(rf"var\s+{name}\s*=\s*'([^']*)'", page)
    return m.group(1) if m else None


def _power_on_one(bot: RenewalBot, order_id: str) -> bool:
    page = _power_page(bot, order_id)
    status = _status_of(page)

    # 页面里全部电源/启停类 subaction 与 JS 变量，便于日志分析
    subs = sorted(set(re.findall(
        r"subaction[\"']?\s*[:=]\s*[\"']([a-zA-Z0-9_]*(?:power|start|stop|boot)[a-zA-Z0-9_]*)[\"']",
        page, re.I)))
    bot.log(f"🔍 合同 {order_id} 电源状态: {status} | 页面启停类 subaction: {subs}")

    if status == "running":
        bot.log(f"✅ 合同 {order_id}: 容器已在运行，无需开机")
        return True

    # 找开机动作: 优先 poweron/power_on/start，排除 poweroff/stop
    candidates = [s for s in subs if not re.search(r"off|stop", s, re.I)]
    if not candidates:
        bot.log(f"❗ 合同 {order_id}: 未解析到开机动作，请人工查看 power_{order_id}.html", LogLevel.WARNING)
        return False

    srv_id = _extract_js_var(page, "srv_id") or ""
    m = re.search(r"'srv_id='\s*\+\s*(\d+)", page)
    if not srv_id and m:
        srv_id = m.group(1)
    ord_no = order_id

    for action in candidates:
        params = f"sess_id={bot.sess_id}&subaction={action}&ord_no={ord_no}"
        if srv_id:
            params += f"&srv_id={srv_id}"
        bot.log(f"🔄 合同 {order_id}: 触发 {action} ...")
        resp = bot.session.get(
            f"{er.EUSERV_BASE_URL}?{params}",
            headers={"user-agent": er.USER_AGENT},
            timeout=er.HTTP_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        content = _parse_ajax_var(resp.text, "server_start_stop") or _parse_ajax_var(resp.text, "server_power") or ""
        rc_m = re.search(r"\(rc/\)(\d+)(/rc)", resp.text)
        rs_m = re.search(r"\(rs/\)(.*?)(/rs)", resp.text, re.S)
        rc = rc_m.group(1) if rc_m else "?"
        msg = re.sub(r"\s+", " ", rs_m.group(1)).strip()[:300] if rs_m else content[:300] or resp.text[:200]
        bot.log(f"   → {action} 响应 rc={rc}: {msg}")
        if rc in ("", "100", "1007") or "already" in msg.lower() or "progress" in msg.lower():
            bot.log(f"✅ 合同 {order_id}: 开机指令已受理 (rc={rc})")
            return True
    return False


def power_account(bot: RenewalBot, target: str, action: str) -> bool:
    bot.log(f"--- Power 账号【{bot.account_name}】action={action} ---")
    bot.prewarm_ocr()
    bot._perform_login()

    servers = bot._get_servers()
    ids = [str(s["id"]) for s in servers]
    if target.isdigit() and len(target) > 2:
        candidates = [target] if target in ids else []
    else:
        candidates = ids
    if not candidates:
        bot.log(f"⏭️ 账号下没有匹配目标 {target} 的合同（现有: {ids}）")
        return True

    ok = True
    for order_id in candidates:
        if action == "probe":
            page = _power_page(bot, order_id)
            bot.log(f"🔍 合同 {order_id} 电源状态: {_status_of(page)} (页面已 dump 到 power_{order_id}.html)")
            resp = bot.session.get(
                f"{er.EUSERV_BASE_URL}"
                f"?sess_id={bot.sess_id}"
                f"&subaction=show_server_rescuesystem",
                headers={"user-agent": er.USER_AGENT},
                timeout=er.HTTP_TIMEOUT_SECONDS,
            )
            resp.raise_for_status()
            with open(f"rescue_{order_id}.html", "w", encoding="utf-8") as f:
                f.write(resp.text)
            bot.log(f"🔍 合同 {order_id} Rescue 页已 dump 到 rescue_{order_id}.html")
            for sub in ("show_kc2_customer_tools", "show_kc2_customer_extended_order_settings", "show_kc2_customer_preferences"):
                resp2 = bot.session.get(
                    f"{er.EUSERV_BASE_URL}"
                    f"?sess_id={bot.sess_id}"
                    f"&subaction={sub}",
                    headers={"user-agent": er.USER_AGENT},
                    timeout=er.HTTP_TIMEOUT_SECONDS,
                )
                fn = f"{sub}_{order_id}.html"
                with open(fn, "w", encoding="utf-8") as f:
                    f.write(resp2.text)
                body = re.sub(r"<[^>]+>", " ", re.sub(r"<script.*?</script>", " ", resp2.text, flags=re.S))
                body = re.sub(r"\s+", " ", body)
                hit = [k for k in ("postinstall", "Postinstall") if k in resp2.text]
                bot.log(f"🔍 {sub} dump 完成, postinstall 命中: {hit}")
        else:
            ok = _power_on_one(bot, order_id) and ok
    return ok


def select_bots(target: str) -> list[RenewalBot]:
    accounts = parse_accounts()
    if accounts:
        bots = [RenewalBot(account=acct) for acct in accounts]
    else:
        bots = [RenewalBot()]
    if target and target.lower() != "all" and len(target) <= 2 and target.isdigit():
        idx = int(target) - 1
        bots = bots[idx:idx + 1]
    return bots


def main() -> int:
    args = sys.argv[1:]
    action = args[0] if args else "probe"
    if action not in ("probe", "on", "poweron"):
        print(f"未知动作: {action} (可用: probe/on)")
        return 2
    target = args[1] if len(args) > 1 else "all"

    bots = select_bots(target)
    if not bots:
        print("没有可用账号（检查 EUSERV_USERNAME 等环境变量）")
        return 2

    results = []
    for bot in bots:
        try:
            results.append(power_account(bot, target, action))
        except Exception as exc:  # noqa: BLE001
            bot.log(f"❗ 账号执行异常: {exc}", LogLevel.ERROR)
            results.append(False)

    success = any(results)
    bot.log(f"===== Power {action} 完成，{'成功' if success else '失败'} =====")
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
