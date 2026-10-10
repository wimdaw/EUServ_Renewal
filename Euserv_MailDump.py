# SPDX-License-Identifier: GPL-3.0-or-later
"""Dump EUServ 最近邮件（用于读取重装后的 root 密码等）。

用法:
  python Euserv_MailDump.py [关键词]   # 默认搜 no-reply@euserv.com 今天的邮件

只读：列出匹配邮件的日期/主题/正文前若干字符。
"""

import email
import imaplib
import re
import sys
from datetime import date

from Euserv_Renewal import RenewalBot, parse_accounts


def decode_body(msg: email.message.Message) -> str:
    def dec(part) -> str:
        charset = part.get_content_charset() or "utf-8"
        payload = part.get_payload(decode=True)
        if payload is None:
            return ""
        return payload.decode(charset, errors="replace") if isinstance(payload, bytes) else str(payload)

    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                return dec(part)
        return ""
    return dec(msg)


def dump_mailbot(bot: RenewalBot, keyword: str) -> None:
    bot.log(f"--- MailDump 账号【{bot.account_name}】keyword={keyword} ---")
    mail = imaplib.IMAP4_SSL(bot.email_host or "imap.gmail.com")
    try:
        mail.login(bot.email_username or "", bot.email_password or "")
        mail.select("inbox")
        today = date.today().strftime("%d-%b-%Y")
        crit = f'(SINCE "{today}" FROM "no-reply@euserv.com")'
        if keyword:
            crit = f'(SINCE "{today}" FROM "no-reply@euserv.com" SUBJECT "{keyword}")'
        status, data = mail.search(None, crit)
        if status != "OK" or not data[0]:
            bot.log("没有匹配的邮件，退化为列出今天全部邮件")
            status, data = mail.search(None, f'(SINCE "{today}")')
            if status != "OK" or not data[0]:
                bot.log("邮箱今天没有邮件")
                return
        ids = data[0].split()[-6:]
        bot.log(f"共 {len(ids)} 封待查邮件")
        for mid in reversed(ids):
            _, fd = mail.fetch(mid, "(RFC822)")
            if not fd or not fd[0] or not isinstance(fd[0], tuple):
                continue
            msg = email.message_from_bytes(fd[0][1])
            subj = str(msg.get("Subject", ""))
            try:
                subj = re.sub(r"=\?[^?]+\?[bBQq]\?[^?]*\?=", lambda m: str(email.header.decode_header(m.group(0))[0][0], errors="replace") if isinstance(email.header.decode_header(m.group(0))[0][0], bytes) else str(email.header.decode_header(m.group(0))[0][0]), subj)
            except Exception:
                pass
            frm = str(msg.get("From", ""))
            date_s = str(msg.get("Date", ""))
            body = decode_body(msg)
            body_text = re.sub(r"<[^>]+>", " ", body)
            body_text = re.sub(r"\s+", " ", body_text).strip()
            bot.log(f"===== 邮件 [{mid.decode()}] {date_s}")
            bot.log(f"  From: {frm}")
            bot.log(f"  Subject: {subj}")
            bot.log(f"  Body: {body_text[:1200]}")
    finally:
        try:
            mail.logout()
        except Exception:
            pass


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else ""
    accounts = parse_accounts()
    bots = [RenewalBot(account=a) for a in accounts] if accounts else [RenewalBot()]
    for bot in bots:
        try:
            dump_mailbot(bot, keyword)
        except Exception as exc:  # noqa: BLE001
            bot.log(f"❗ MailDump 失败: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
