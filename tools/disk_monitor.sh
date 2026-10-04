#!/bin/bash
# 磁盘监控v2: 绝对可用空间制(30G小盘不适合百分比阈值)
# 可用<800M: 自动清理; <400M: 微信报警
LOG=/root/nono-mind/tools/disk_monitor.log
AVAIL_KB=$(df --output=avail / | tail -1)
AVAIL_MB=$((AVAIL_KB / 1024))
echo "$(date '+%F %T') 可用${AVAIL_MB}M" >> $LOG

if [ "$AVAIL_MB" -lt 800 ]; then
  echo "$(date '+%F %T') 触发自动清理(<800M)" >> $LOG
  rm -rf /root/.cache/pip /root/.cache/node-gyp /root/.hermes/skills/.curator_backups 2>/dev/null
  rm -rf /root/nono-workbench/target/release /root/.cargo/registry/cache 2>/dev/null
  find /tmp -type f -atime +7 -delete 2>/dev/null
  journalctl --vacuum-size=20M >/dev/null 2>&1
  AVAIL_KB=$(df --output=avail / | tail -1)
  AVAIL_MB=$((AVAIL_KB / 1024))
  echo "$(date '+%F %T') 清理后${AVAIL_MB}M" >> $LOG
fi

if [ "$AVAIL_MB" -lt 400 ]; then
  # 只写State信号, 不直接发微信(微信通道等新身体自己的号接入后再用)
  PYTHONPATH=/root/nono-mind python3 -c "
from state.server import StateClient
c = StateClient()
c.emit('system_alert', {'type': 'disk', 'avail_mb': ${AVAIL_MB}}, source='disk_monitor', priority='P1')
c.close()" >> $LOG 2>&1
  sleep 60
fi
