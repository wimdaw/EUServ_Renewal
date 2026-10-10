#!/bin/bash
# 桥接脚本: 在节点2(IPv6 WARP 出口)上执行, 链式 SSH 到 EUServ v6 机器
# 需要 env: EUSERV_PASSWORD(面板密码候选) EXTRA_PASSWORDS(逗号分隔候选)
set -u
export DEBIAN_FRONTEND=noninteractive
command -v sshpass >/dev/null || { apt-get update -qq >/dev/null 2>&1; apt-get install -y -qq sshpass >/dev/null 2>&1; }

TARGET=2a02:180:6:1::13
FOUND=""

IFS=',' read -ra PWS <<< "$EUSERV_PASSWORD,$EXTRA_PASSWORDS"
for pw in "${PWS[@]}"; do
  [ -z "$pw" ] && continue
  echo "[try] ${pw:0:3}***"
  if sshpass -p "$pw" ssh -6 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=15 root@$TARGET 'echo LOGIN-OK' 2>/dev/null | grep -q LOGIN-OK; then
    echo "=== LOGIN OK with ${pw:0:3}*** ==="
    FOUND="$pw"
    break
  fi
done

if [ -z "$FOUND" ]; then
  echo "!!! ALL PASSWORDS FAILED"
  exit 1
fi

sshpass -p "$FOUND" ssh -6 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
  root@$TARGET 'bash -s' <<'EOF2'
export DEBIAN_FRONTEND=noninteractive
echo 'root:LaedX3wR' | chpasswd && echo "root password set to LaedX3wR"
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq curl wget >/dev/null 2>&1
cd /root && curl -so OsMutation.sh https://raw.githubusercontent.com/hackerxiaow/OsMutation-fix/main/OsMutation.sh
chmod +x OsMutation.sh
echo "=== START REINSTALL ==="
OS_INDEX=3 VIRT_TYPE=lxc ./OsMutation.sh
RC=$?
echo "=== SCRIPT EXIT: $RC ==="
if [ $RC -eq 0 ]; then sync; sleep 1; reboot -f; fi
EOF2
echo "=== bridge done ==="
