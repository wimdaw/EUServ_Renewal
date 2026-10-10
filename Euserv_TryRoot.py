# SPDX-License-Identifier: GPL-3.0-or-later
"""尝试用候选密码 SSH 登录 root，成功则执行 Alpine 重装序列。

候选密码: EUSERV_PASSWORD(面板密码, EUServ 免费机重装后 root 密码常等于它)
          + SSH_ROOT_PASSWORDS(逗号分隔的额外候选)
"""

import os
import sys

import paramiko

HOST = "2a02:180:6:1::13"

CMDS = r"""
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq && apt-get install -y -qq curl wget >/dev/null 2>&1
cd /root && curl -so OsMutation.sh https://raw.githubusercontent.com/hackerxiaow/OsMutation-fix/main/OsMutation.sh && chmod +x OsMutation.sh
echo "=== START REINSTALL ==="
OS_INDEX=3 VIRT_TYPE=lxc ./OsMutation.sh
RC=$?
echo "=== SCRIPT EXIT: $RC ==="
if [ $RC -eq 0 ]; then sync; sleep 1; reboot -f; fi
"""


def try_login(password: str) -> paramiko.SSHClient | None:
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        c.connect(HOST, 22, username="root", password=password, timeout=15,
                  allow_agent=False, look_for_keys=False, banner_timeout=15)
        return c
    except Exception as e:  # noqa: BLE001
        print(f"[try] {password[:3]}*** -> {e}")
        try:
            c.close()
        except Exception:
            pass
        return None


def main() -> int:
    cands = [os.environ.get("EUSERV_PASSWORD", "")]
    cands += [p for p in os.environ.get("SSH_ROOT_PASSWORDS", "").split(",") if p]
    cands = [p for p in cands if p]

    for pw in cands:
        print(f"[try] {pw[:3]}***")
        client = try_login(pw)
        if client is None:
            continue
        print(f"✅ 登录成功 (密码 {pw[:3]}***)，开始执行重装序列 ...")
        stdin, stdout, stderr = client.exec_command(CMDS, timeout=900)
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        print("--- stdout tail ---")
        print(out[-3500:])
        if err.strip():
            print("--- stderr tail ---")
            print(err[-1200:])
        client.close()
        return 0 if "SCRIPT EXIT: 0" in out else 1
    print("❗ 所有候选密码均登录失败")
    return 1


if __name__ == "__main__":
    sys.exit(main())
