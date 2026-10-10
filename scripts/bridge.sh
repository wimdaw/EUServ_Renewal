#!/bin/bash
# 桥接脚本: 在节点2(IPv6 WARP)上执行, 链式 SSH 到 EUServ v6
# 模式: 若登录成功且发现原盘挂载(rescue 环境)则修复 root 密码; 否则执行重装序列
set -u
export DEBIAN_FRONTEND=noninteractive
command -v sshpass >/dev/null || { apk add sshpass >/dev/null 2>&1 || apt-get install -y sshpass >/dev/null 2>&1; }

TARGET=2a02:180:6:1::13
FOUND=""

IFS=',' read -ra PWS <<< "$EUSERV_PASSWORD,$EXTRA_PASSWORDS"
for pw in "${PWS[@]}"; do
  [ -z "$pw" ] && continue
  echo "[try] ${pw:0:3}***"
  if sshpass -p "$pw" ssh -6 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o ConnectTimeout=15 -o PreferredAuthentications=password -o PubkeyAuthentication=no \
      root@$TARGET 'echo LOGIN-OK' 2>/dev/null | grep -q LOGIN-OK; then
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
echo "=== ENV DUMP ==="
uname -a
head -2 /etc/os-release 2>/dev/null
echo "--- mount points ---"
ls /mnt /media /target 2>/dev/null
df -h 2>/dev/null | grep -vE "tmpfs|devtmpfs" | head -8
echo "--- find original root ---"
RD=""
for d in /mnt/* /media/* /target; do
  if [ -e "$d/etc/os-release" ]; then RD="$d"; echo "FOUND ORIG ROOT: $d"; head -1 "$d/etc/os-release"; break; fi
done
if [ -n "$RD" ]; then
  echo 'root:LaedX3wR' | chroot "$RD" /bin/sh -c 'chpasswd' && echo "ORIG ROOT PASSWORD SET TO LaedX3wR"
else
  echo "no original root found (probably booted normal system)"
fi
echo "=== done ==="
EOF2
echo "=== bridge done ==="
