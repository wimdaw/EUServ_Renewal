# SPDX-License-Identifier: GPL-3.0-or-later
"""EUServ 面板网页端重启 (webreboot)。

用法:
  python Euserv_WebReboot.py probe [目标]          # 只读探测面板重启入口
  python Euserv_WebReboot.py reboot [目标] [理由]   # 网页端强制重启

目标: 账号序号(1/2)、合同号(如 450559)或 all，默认 all
理由: 1=Maintenance(planned) 2=Server does not respond 3=Other，默认 2

设计目标：机器 SSH 失联时也能远程强制重启——整个流程只依赖
EUServ 控制面板 (support.euserv.com) 的 HTTP 接口，与 VPS 本身是否存活无关。

面板接口（2026-10 实测逆向）:
  1. POST index.iphp  subaction=choose_order & ord_no=<合同号>      # 选中合同
  2. GET  index.iphp?sess_id=..&subaction=kc2_server_reset_init      # Webreboot 页
     页面 JS 内含 srv_id / ord_no
  3. GET  index.iphp?sess_id=..&subaction=kc2_server_reset_do_reset
         &server_reset_reason_type=<1|2|3> &srv_id=<srv_id> &ord_no=<合同号>
     响应为 ajax JS，含 server_reset 变量（结果 HTML），无需邮件 PIN。
"""

import re
import sys
import time

from bs4 import BeautifulSoup

import Euserv_Renewal as er
from Euserv_Renewal import LogLevel, RenewalBot, parse_accounts

# 探测关键词（大小写不敏感）
KEYWORDS = (
    "reset",
    "reboot",
    "restart",
    "neustart",
    "neu starten",
    "rescue",
    "vserver",
    "v-server",
    "webreboot",
)

CTX_SNIPPET_LIMIT = 60  # 每个页面最多输出的关键词上下文片段数


def _strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment)).strip()


def extract_candidates(html: str) -> list[str]:
    """从页面 HTML 中提取所有疑似重启/重置入口，返回人类可读的候选列表。"""
    found: list[str] = []

    # 1) 关键词 <a> 链接（href + 文本）
    for m in re.finditer(r"<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>", html, re.S | re.I):
        href, text = m.group(1), _strip_tags(m.group(2))
        blob = f"{href} {text}".lower()
        if any(k in blob for k in KEYWORDS):
            found.append(f"[a] href={href[:220]}  text={text[:90]}")

    # 2) 页面里出现的所有 subaction 值（表单 hidden / JS 参数）
    for m in re.finditer(r"subaction[\"']?\s*[:=]\s*[\"']([a-zA-Z0-9_]+)[\"']", html):
        found.append(f"[subaction] {m.group(1)}")

    # 3) 含关键词的表单（action + hidden inputs + 按钮）
    for m in re.finditer(r"<form[^>]*>(.*?)</form>", html, re.S | re.I):
        body = m.group(1)
        if any(k in body.lower() for k in KEYWORDS):
            action = re.search(r"<form[^>]*action=\"([^\"]*)\"", m.group(0), re.I)
            found.append(f"[form] action={action.group(1) if action else '-'}")
            for inp in re.finditer(r"<input[^>]*>", body, re.I):
                tag = _strip_tags(inp.group(0))
                if tag:
                    found.append(f"    input: {tag[:200]}")

    # 4) 按钮/onclick 文本命中
    for m in re.finditer(r"<(button|input)[^>]*>", html, re.I):
        tag = m.group(0)
        if any(k in tag.lower() for k in KEYWORDS):
            found.append(f"[button] {_strip_tags(tag)[:220]}")

    # 5) 关键词上下文片段（还原真实字段名最直接的证据）
    ctx_seen: set[str] = set()
    ctx_count = 0
    for m in re.finditer(
        r"(?i)(reset|reboot|restart|neustart|webreboot|rescue|vserver)", html
    ):
        if ctx_count >= CTX_SNIPPET_LIMIT:
            break
        s, e = max(0, m.start() - 160), min(len(html), m.end() + 160)
        snippet = re.sub(r"\s+", " ", html[s:e]).strip()
        if snippet in ctx_seen:
            continue
        ctx_seen.add(snippet)
        found.append(f"[ctx] …{snippet}…")
        ctx_count += 1

    # 去重保序
    deduped: list[str] = []
    seen: set[str] = set()
    for line in found:
        if line not in seen:
            seen.add(line)
            deduped.append(line)
    return deduped


def dump_nav_links(bot: RenewalBot, html: str) -> None:
    """输出侧边导航链接，便于理解面板结构。"""
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        text = _strip_tags(a.get_text())[:40]
        href = a["href"][:120]
        if text:
            links.append(f"{text} -> {href}")
    bot.log(f"页面导航链接共 {len(links)} 条（前 60 条）：")
    for line in links[:60]:
        bot.log(f"   {line}")


def _choose_order(bot: RenewalBot, order_id: str) -> str:
    """选中合同并返回详情页 HTML。"""
    step1 = bot.session.post(
        er.EUSERV_BASE_URL,
        headers={
            "user-agent": er.USER_AGENT,
            "Host": "support.euserv.com",
            "origin": er.EUSERV_ORIGIN,
        },
        data={
            "Submit": "Extend contract",
            "sess_id": bot.sess_id,
            "ord_no": order_id,
            "subaction": "choose_order",
            "choose_order_subaction": "show_contract_details",
        },
        timeout=er.HTTP_TIMEOUT_SECONDS,
    )
    step1.raise_for_status()
    return step1.text


def _get_reset_page(bot: RenewalBot):
    """访问 Webreboot 初始化页，返回 Response 对象。"""
    return bot.session.get(
        f"{er.EUSERV_BASE_URL}?sess_id={bot.sess_id}&subaction=kc2_server_reset_init",
        headers={"user-agent": er.USER_AGENT},
        timeout=er.HTTP_TIMEOUT_SECONDS,
    )


def _parse_ajax_var(html: str, name: str) -> str:
    """从 kc2 ajax 响应里提取 (name/)value(/name) 标签值。"""
    m = re.search(rf"\({name}/\)(.*?)(/{name})", html, re.S)
    if not m:
        return ""
    value = m.group(1)
    value = value.encode().decode("unicode_escape", errors="replace")
    return value


def probe_account(bot: RenewalBot) -> bool:
    """只读探测单个账号面板中的重启入口。返回是否成功完成探测。"""
    bot.log(f"--- 探测账号【{bot.account_name}】---")
    bot.prewarm_ocr()
    bot._perform_login()

    # 概览页（服务器列表）
    overview = bot.session.get(
        f"{er.EUSERV_BASE_URL}?sess_id={bot.sess_id}",
        headers={"user-agent": er.USER_AGENT},
        timeout=er.HTTP_TIMEOUT_SECONDS,
    )
    overview.raise_for_status()
    with open("probe_overview.html", "w", encoding="utf-8") as f:
        f.write(overview.text)

    bot.log("========== 概览页候选 ==========")
    for line in extract_candidates(overview.text):
        bot.log(line)
    dump_nav_links(bot, overview.text)

    servers = bot._get_servers()
    for srv in servers:
        order_id = srv["id"]
        bot.log(f"========== 合同 {order_id} 详情页候选 ==========")
        detail = _choose_order(bot, order_id)
        with open(f"probe_contract_{order_id}.html", "w", encoding="utf-8") as f:
            f.write(detail)
        for line in extract_candidates(detail):
            bot.log(line)

        # 详情页若带 Webreboot 导航，则继续只读访问 reset_init 页面，
        # 摸清确认表单结构（GET 仅展示，无副作用）
        if "kc2_server_reset_init" in detail:
            bot.log(f"---------- 合同 {order_id} Webreboot 初始化页 ----------")
            reset_page = _get_reset_page(bot)
            reset_page.raise_for_status()
            with open(f"probe_reset_{order_id}.html", "w", encoding="utf-8") as f:
                f.write(reset_page.text)
            for line in extract_candidates(reset_page.text):
                bot.log(line)
            soup = BeautifulSoup(reset_page.text, "html.parser")
            main = soup.select_one("#main_content") or soup.body
            if main:
                text = re.sub(r"\s{2,}", "\n", main.get_text("\n"))
                bot.log(f"[reset 页可见文本]\n{text[:1500]}")
        else:
            bot.log(f"合同 {order_id}: 面板未提供 Webreboot 导航（跳过 reset 探测）")

        # 只读探测重装页：系统镜像列表与确认表单
        if "show_kc2_server_reinstall" in detail:
            bot.log(f"---------- 合同 {order_id} 重装(Reinstallation)初始化页 ----------")
            page = bot.session.get(
                f"{er.EUSERV_BASE_URL}?sess_id={bot.sess_id}&subaction=show_kc2_server_reinstall",
                headers={"user-agent": er.USER_AGENT},
                timeout=er.HTTP_TIMEOUT_SECONDS,
            )
            page.raise_for_status()
            with open(f"probe_reinstall_{order_id}.html", "w", encoding="utf-8") as f:
                f.write(page.text)
            soup = BeautifulSoup(page.text, "html.parser")
            main = soup.select_one("#main_content") or soup.body
            if main:
                text = re.sub(r"\s{2,}", "\n", main.get_text("\n"))
                bot.log(f"[reinstall 页可见文本]\n{text[:2000]}")
            for sel in main.select("select") if main else []:
                opts = [(o.get("value", ""), o.get_text(strip=True)[:40]) for o in sel.select("option")]
                bot.log(f"[select] name={sel.get('name')} id={sel.get('id')} 选项: {opts}")
            for inp in main.select("input, button") if main else []:
                tag = _strip_tags(str(inp))[:200]
                if tag:
                    bot.log(f"[input] {tag}")
            # 找 JS 里的 reinstall subaction
            for m in set(re.findall(r"(?i)subaction[\"']?\s*[:=]\s*[\"']([a-z0-9_:]*reinstall[a-z0-9_:]*)[\"']", page.text)):
                bot.log(f"[reinstall subaction] {m}")
    return True


def _webreboot_one(bot: RenewalBot, order_id: str, reason_type: str) -> bool:
    """对单个合同执行网页端重启。返回是否已触发。"""
    detail = _choose_order(bot, order_id)
    if "kc2_server_reset_init" not in detail:
        bot.log(
            f"⏭️ 合同 {order_id}: 面板未提供 Webreboot 入口（vServer 管理菜单未渲染），跳过",
            LogLevel.WARNING,
        )
        return False

    reset_page = _get_reset_page(bot)
    reset_page.raise_for_status()
    srv_m = re.search(r"'srv_id='\s*\+\s*(\d+)", reset_page.text)
    ord_m = re.search(r"'ord_no='\s*\+\s*(\d+)", reset_page.text)
    if not srv_m or not ord_m:
        raise RuntimeError(f"合同 {order_id}: reset 页解析不到 srv_id/ord_no，页面结构可能已变化")
    srv_id = srv_m.group(1)
    page_ord = ord_m.group(1)
    if page_ord != str(order_id):
        bot.log(f"⚠️ reset 页 ord_no={page_ord} 与目标 {order_id} 不一致，以页面为准继续", LogLevel.WARNING)

    bot.log(
        f"🔄 合同 {order_id} (srv_id={srv_id}) 正在触发 Webreboot (reason_type={reason_type})..."
    )
    params = (
        f"sess_id={bot.sess_id}"
        f"&subaction=kc2_server_reset_do_reset"
        f"&server_reset_reason_type={reason_type}"
        f"&srv_id={srv_id}"
        f"&ord_no={page_ord}"
    )
    # 与浏览器 exec_async 一致只发一次 GET，do_reset 是动作型接口严禁重复提交
    resp = bot.session.get(
        f"{er.EUSERV_BASE_URL}?{params}",
        headers={"user-agent": er.USER_AGENT},
        timeout=er.HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()

    content = _parse_ajax_var(resp.text, "server_reset")
    if content:
        clean = re.sub(r"\s+", " ", content).strip()
        if "error" in clean.lower() or "fail" in clean.lower():
            bot.log(f"❗ 合同 {order_id}: 面板返回错误信息: {clean[:300]}", LogLevel.WARNING)
            raise RuntimeError(f"合同 {order_id}: 面板拒绝重启: {clean[:200]}")
        bot.log(f"✅ 合同 {order_id} Webreboot 已触发，面板响应: {clean[:400]}")
        return True

    # 无 server_reset 内容时按 rc/rs 错误协议解析
    rc_m = re.search(r"\(rc/\)(\d+)(/rc)", resp.text)
    rs_m = re.search(r"\(rs/\)(.*?)(/rs)", resp.text, re.S)
    rc = rc_m.group(1) if rc_m else "?"
    msg = rs_m.group(1).strip() if rs_m else resp.text[:200]
    if rc == "1007" or "already running" in msg.lower():
        bot.log(
            f"⏳ 合同 {order_id}: 面板提示该服务器已有任务在执行（很可能是刚才的重启指令尚未完成），无需重复下发"
        )
        return True
    bot.log(f"❗ 合同 {order_id}: 面板返回 rc={rc}: {msg[:300]}", LogLevel.WARNING)
    raise RuntimeError(f"合同 {order_id}: Webreboot 失败 rc={rc}: {msg[:200]}")


def reboot_account(bot: RenewalBot, target: str, reason_type: str) -> bool:
    """对单个账号下目标合同执行网页端重启。"""
    bot.log(f"--- Webreboot 账号【{bot.account_name}】---")
    bot.prewarm_ocr()
    bot._perform_login()

    servers = bot._get_servers()
    targets = [s["id"] for s in servers if target == "all" or str(s["id"]) == str(target)]
    if not targets:
        bot.log(f"⏭️ 账号下没有匹配目标 {target} 的合同（现有: {[s['id'] for s in servers]}）")
        return True

    triggered = False
    for order_id in targets:
        try:
            if _webreboot_one(bot, order_id, reason_type):
                triggered = True
        except Exception as e:  # noqa: BLE001 —— 单台失败不影响其余目标
            bot.log(f"❗ 合同 {order_id} 重启失败: {e}", LogLevel.WARNING)
            time.sleep(3)
    if triggered:
        bot.log("🎉 全部目标 Webreboot 指令已下发，等待机器重启（约 1-5 分钟后恢复网络）")
    return True


def _reinstall_details(bot: RenewalBot, ord_no: str, lang: str = "en") -> dict:
    """加载重装详情（OS 列表 / 安装设备），只读。"""
    resp = bot.session.get(
        f"{er.EUSERV_BASE_URL}"
        f"?sess_id={bot.sess_id}"
        f"&subaction=kc2_server_reinstall_get_details"
        f"&kc2_server_reinstall_action=show_default_modules_html"
        f"&osfl_lang={lang}&os_type=normal",
        headers={"user-agent": er.USER_AGENT},
        timeout=er.HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    text = resp.text

    # OS 单选列表: <input ... name="kc2_server_reinstall_os_radio" value="<srv_os_id>" ...> + 邻近 label
    os_list: list[dict] = []
    for m in re.finditer(r"<input[^>]*name=\"kc2_server_reinstall_os_radio\"[^>]*>", text):
        tag = m.group(0)
        val_m = re.search(r"value=\"([^\"]*)\"", tag)
        if not val_m:
            continue
        # 取该 radio 到下一个 radio 之间的文本作为名称
        nxt = re.search(r"<input[^>]*name=\"kc2_server_reinstall_os_radio\"", text[m.end():])
        seg = text[m.end(): m.end() + nxt.start()] if nxt else text[m.end(): m.end() + 1500]
        seg = re.split(r"\(/kc2_server_reinstall|</tr>", seg)[0]
        name = _strip_tags(seg).strip(" :")[:80]
        os_list.append({"id": val_m.group(1), "name": name or "(未知名)"})

    # 安装设备下拉（可选）
    devices: list[dict] = []
    sel = re.search(
        r"<select[^>]*name=\"kc2_server_reinstall_select_install_device\"[^>]*>(.*?)</select>",
        text, re.S,
    )
    if sel:
        for o in re.finditer(r"<option[^>]*value=\"([^\"]*)\"[^>]*>([^<]*)", sel.group(1)):
            devices.append({"id": o.group(1), "name": _strip_tags(o.group(2))})

    return {"os_list": os_list, "devices": devices}


def _reinstall_order(bot: RenewalBot, ord_no: str, os_id: str, confirm: bool, lang: str = "en") -> dict:
    """提交重装订单。confirm=False 时仅询价（面板返回 rc=1030 与支付明细，不会执行）。"""
    params = (
        f"sess_id={bot.sess_id}"
        f"&subaction=kc2_server_reinstall_do_reinstall"
        f"&ord_id={ord_no}"
        f"&os_lang={lang}"
        f"&srv_os_id={os_id}"
        f"&wipe_hdds=1"
        f"&reset_bootdevice=1"
    )
    if confirm:
        params += "&confirm_os_license_payment=1"
    resp = bot.session.get(
        f"{er.EUSERV_BASE_URL}?{params}",
        headers={"user-agent": er.USER_AGENT},
        timeout=er.HTTP_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {"rc": "PARSE_ERROR", "rs": resp.text[:400]}


def reinstall_account(bot: RenewalBot, target: str, os_arg: str, confirm: bool, lang: str = "en") -> bool:
    """重装流程：list 模式列系统；给 os_id 则下单（先询价）。"""
    bot.log(f"--- Reinstall 账号【{bot.account_name}】target={target} ---")
    bot.prewarm_ocr()
    bot._perform_login()

    servers = bot._get_servers()
    # 解析目标合同：合同号(>2位数字)精确匹配；1/2/all 时取该账号全部合同
    ids = [str(s["id"]) for s in servers]
    if target.isdigit() and len(target) > 2:
        candidates = [target] if target in ids else []
    else:
        candidates = ids
    if not candidates:
        bot.log(f"⏭️ 账号下没有匹配目标 {target} 的合同（现有: {ids}）")
        return True

    capable = []
    for order_id in candidates:
        detail = _choose_order(bot, order_id)
        if "show_kc2_server_reinstall" in detail:
            capable.append(order_id)
        else:
            bot.log(f"⏭️ 合同 {order_id}: 面板未提供 Reinstallation 入口，跳过")
    if not capable:
        bot.log("❗ 没有任何合同支持重装", LogLevel.WARNING)
        return False

    order_id = capable[0]
    details = _reinstall_details(bot, order_id, lang)
    os_list = details["os_list"]
    if not os_list:
        bot.log(f"❗ 合同 {order_id}: 未能解析出可安装系统列表（页面结构可能已变化）", LogLevel.WARNING)
        return False

    devices = details["devices"]
    if devices:
        bot.log(f"安装设备: {[(d['id'], d['name']) for d in devices]}")

    if os_arg in ("", "list", "-"):
        bot.log(f"📋 合同 {order_id} 可安装系统（os_lang={lang}），共 {len(os_list)} 项：")
        for o in os_list:
            bot.log(f"   os_id={o['id']}  |  {o['name']}")
        bot.log(f"下单命令: /reinstall {order_id} <os_id> [confirm]  ⚠️ 重装会清空全部数据")
        return True

    matched = [o for o in os_list if o["id"] == os_arg]
    if not matched:
        bot.log(f"❗ os_id={os_arg} 不在可安装列表中，可用: {[(o['id'], o['name']) for o in os_list]}")
        return False
    os_name = matched[0]["name"]

    result = _reinstall_order(bot, order_id, os_arg, confirm, lang)
    rc = str(result.get("rc", "?"))
    if rc == "1030":
        bot.log(f"💰 合同 {order_id}: 重装【{os_name}】需要支付确认，面板返回支付明细:")
        bot.log(f"   {str(result.get('payments'))[:600]}")
        bot.log("⚠️ 数据将被全部清空且可能产生费用！确认执行请再发: /reinstall "
                f"{order_id} {os_arg} confirm")
        return True
    if rc == "100":
        msg = re.sub(r"<[^>]+>|\s+", " ", str(result.get("rs", "")))[:300]
        bot.log(f"❗ 合同 {order_id}: 面板拒绝重装: {msg}", LogLevel.WARNING)
        return False
    if rc == "PARSE_ERROR":
        bot.log(f"❗ 合同 {order_id}: 响应解析失败: {result.get('rs')}", LogLevel.WARNING)
        return False

    msg = re.sub(r"<[^>]+>|\s+", " ", str(result.get("rs", "")))[:300]
    bot.log(f"{'✅' if confirm else 'ℹ️'} 合同 {order_id} 重装【{os_name}】下单结果 rc={rc}: {msg}")
    if confirm:
        bot.log("🎉 重装指令已下发，系统将全盘清空并重装，机器会离线一段时间后以全新系统上线")
    return True


def select_bots(target: str) -> list[RenewalBot]:
    """按目标序号(1/2)或 all 选择账号机器人；合同号目标时返回全部账号（后续按合同过滤）。"""
    accounts = parse_accounts()
    if accounts:
        bots = [RenewalBot(account=acct) for acct in accounts]
    else:
        bots = [RenewalBot()]
    # 纯序号（1/2）时选择对应账号；合同号(如 450559)或 all 时保留全部，按合同过滤
    if target and target.lower() != "all" and len(target) <= 2 and target.isdigit():
        idx = int(target) - 1
        if idx < 0 or idx >= len(bots):
            raise SystemExit(f"目标序号 {target} 超出范围（共 {len(bots)} 个账号）")
        bots = [bots[idx]]
    return bots


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    target = sys.argv[2] if len(sys.argv) > 2 else "all"
    arg3 = sys.argv[3] if len(sys.argv) > 3 else ""
    arg4 = sys.argv[4] if len(sys.argv) > 4 else ""
    arg5 = sys.argv[5] if len(sys.argv) > 5 else "en"

    if mode not in ("probe", "reboot", "reinstall"):
        raise SystemExit(f"未知模式: {mode}（支持 probe / reboot / reinstall）")
    if mode == "reinstall":
        os_arg = arg3 if arg3 else "list"
        confirm = arg4.lower() == "confirm"
    else:
        if arg3 and arg3 not in ("1", "2", "3"):
            raise SystemExit(f"未知重启理由: {arg3}（1=Maintenance 2=Server does not respond 3=Other）")

    bots = select_bots(target)
    # target 为账号序号(1/2)时账号已由 select_bots 选中，合同维度按 all 处理；
    # target 为合同号或 all 时按合同号过滤。
    contract_target = (
        "all"
        if (not target or target.lower() == "all" or (target.isdigit() and len(target) <= 2))
        else target
    )
    ok = True
    for bot in bots:
        try:
            if mode == "probe":
                probe_account(bot)
            elif mode == "reboot":
                reboot_account(bot, contract_target, arg3 or "2")
            else:
                reinstall_account(bot, contract_target, os_arg, confirm, arg5 or "en")
        except Exception as e:  # noqa: BLE001 —— 顶层兜底，保证日志与 TG 报告完整
            bot.log(f"❗ 执行失败: {e}", LogLevel.WARNING)
            ok = False
        finally:
            bot._cleanup()
            bot.send_telegram_notification("成功" if ok else "失败")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
