# SPDX-License-Identifier: GPL-3.0-or-later
"""EUServ 面板网页端重启 (webreboot)。

用法:
  python Euserv_WebReboot.py probe             # 只读探测：登录面板，抓出所有疑似重启入口
  python Euserv_WebReboot.py reboot [目标]      # 对指定账号(序号 1/2 或 all)执行网页端重启

设计目标：机器 SSH 失联时也能远程强制重启——整个流程只依赖
EUServ 控制面板 (support.euserv.com) 的 HTTP 接口，与 VPS 本身是否存活无关。
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
        with open(f"probe_contract_{order_id}.html", "w", encoding="utf-8") as f:
            f.write(step1.text)
        for line in extract_candidates(step1.text):
            bot.log(line)

        # 详情页若带 Webreboot 导航，则继续只读访问 reset_init 页面，
        # 摸清确认表单结构（GET 仅展示，无副作用）
        if "kc2_server_reset_init" in step1.text:
            bot.log(f"---------- 合同 {order_id} Webreboot 初始化页 ----------")
            reset_page = bot.session.get(
                f"{er.EUSERV_BASE_URL}?sess_id={bot.sess_id}&subaction=kc2_server_reset_init",
                headers={"user-agent": er.USER_AGENT},
                timeout=er.HTTP_TIMEOUT_SECONDS,
            )
            reset_page.raise_for_status()
            with open(f"probe_reset_{order_id}.html", "w", encoding="utf-8") as f:
                f.write(reset_page.text)
            for line in extract_candidates(reset_page.text):
                bot.log(line)
            # 完整输出主内容区可见文本，便于判断确认按钮与说明
            soup = BeautifulSoup(reset_page.text, "html.parser")
            main = soup.select_one("#main_content") or soup.body
            if main:
                text = re.sub(r"\s{2,}", "\n", main.get_text("\n"))
                bot.log(f"[reset 页可见文本]\n{text[:1500]}")
        else:
            bot.log(f"合同 {order_id}: 面板未提供 Webreboot 导航（跳过 reset 探测）")
    return True


def reboot_account(bot: RenewalBot) -> bool:
    """对单个账号的所有 VPS 执行网页端重启（探明入口后实现）。"""
    raise NotImplementedError("webreboot 流程待 probe 探明后实现")


def select_bots(target: str) -> list[RenewalBot]:
    """按目标序号(1/2)或 all 选择账号机器人。"""
    accounts = parse_accounts()
    if accounts:
        bots = [RenewalBot(account=acct) for acct in accounts]
    else:
        bots = [RenewalBot()]
    if target and target.lower() != "all":
        idx = int(target) - 1
        if idx < 0 or idx >= len(bots):
            raise SystemExit(f"目标序号 {target} 超出范围（共 {len(bots)} 个账号）")
        bots = [bots[idx]]
    return bots


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "probe"
    target = sys.argv[2] if len(sys.argv) > 2 else "all"

    if mode not in ("probe", "reboot"):
        raise SystemExit(f"未知模式: {mode}（支持 probe / reboot）")

    bots = select_bots(target)
    ok = True
    for bot in bots:
        try:
            if mode == "probe":
                probe_account(bot)
            else:
                reboot_account(bot)
        except Exception as e:  # noqa: BLE001 —— 顶层兜底，保证日志与 TG 报告完整
            bot.log(f"❗ 执行失败: {e}", LogLevel.WARNING)
            ok = False
        finally:
            bot._cleanup()
            bot.send_telegram_notification("成功" if ok else "失败")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
