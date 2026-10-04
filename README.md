# 路由看板 · iNextOS Router Dashboard

一个**单文件、零依赖**的路由器实时监控看板：只用 Python3 标准库，不需要 pip 安装任何东西。
跑在路由器上，局域网内任何设备打开 `http://<路由IP>:9090` 即可查看，**3 秒自动刷新**。

## 一键部署（在任何 iNextOS / Debian / Ubuntu 路由上）

```bash
curl -fsSL https://cdn.jsdelivr.net/gh/a6726170/roceos-dash@main/install.sh | bash
```

备用地址：

```bash
# GitHub raw
curl -fsSL https://raw.githubusercontent.com/a6726170/roceos-dash/main/install.sh | bash

# 直连镜像站（国内可用，.sh 会被 WAF 拦，所以用 .txt）
curl -fsSL https://inextos-dash.app.workbuddy.host/install.txt | bash
```

脚本会自动完成：找 python3 → 写文件到 `/opt/roceos-dash/` → 语法自检 → 创建并启用 systemd
服务 → 防火墙放行端口 → 健康检查 → 打印访问地址。

| 常用操作 | 命令 |
|---|---|
| 换端口重装 | `ROCEOS_DASH_PORT=8080 bash /opt/roceos-dash/install.sh` |
| 看状态 | `systemctl status roceos-dash` |
| 看日志 | `journalctl -u roceos-dash -f` |
| 卸载 | `systemctl disable --now roceos-dash && rm -rf /opt/roceos-dash /etc/systemd/system/roceos-dash.service` |

> 端口被占用时会自动顺延（9090 → 9091 …），实际端口显示在页面顶部。

## 看板内容

- **CPU / 内存**：两个半圆仪表盘并排，下面配条形条；CPU 侧带每核占用、温度、进程数，内存侧带缓存与 SWAP
- **系统负载**：1 / 5 / 15 分钟负载 + 趋势曲线、CPU 近期均值、网口统计
- **网络总览**：实时下行 / 上行 —— **只统计经过 WAN 口的流量**，局域网内互访不经过 WAN，不计入
- **网口实时流量**：WAN 口、WAN 物理承载口、各 LAN 口，分别给出实时 ↓/↑ + 趋势折线、累计流量、MAC、IP、MTU、包数、错误数；
  虚拟网卡（docker0 / virbr0 / ZeroTier …）只在真正工作时才出现
- **广域网 WAN**：接入类型、连接状态、**WAN 口 IP**、**DNS 列表（含来源文件）**、网关 / 对端、MTU、拨号时长、累计与实时流量
- **服务状态**：只列出本机真实存在的服务单元

## 数据来源

| 数据 | 来源 |
|---|---|
| CPU / 内存 / 负载 / 温度 | `/proc/stat`、`/proc/meminfo`、`/proc/loadavg`、`/sys/class/thermal` |
| 网口流量 | 一次读 `/proc/net/dev`（含收发字节 / 包数 / 错误数） |
| 默认路由 / WAN 口 | `/proc/net/route` |
| WAN 类型 / DNS | `/etc/ppp/peers/*`、`/etc/resolv.conf` 或 PPP 下发的 |
| PPPoE 拨号时长 | 扫 `/proc/*/cmdline` 找 pppd + `/proc/*/stat` 第 22 字段 |

## 省电与占用

- **没人看就降频**：超过 `ROCEOS_DASH_IDLE_AFTER`（默认 45 秒）没有 HTTP 请求，
  采样间隔从 1 秒放宽到 `ROCEOS_DASH_IDLE_TICK`（默认 8 秒）并停掉连接跟踪解析；
  一旦有人打开页面立即唤醒恢复 1 秒精度。
- 每次请求尽量**零子进程**：路由表、ARP、pppd 时长、CPU 型号都直接读 `/proc`，
  地址 / WAN 类型 / 拨号时长按 TTL 缓存（5s / 300s / 30s）。
- 实测稳态（无人访问）：**0.02% ~ 0.17% 单核**，内存约 25 MB，`/api/stats` 响应 1~2 ms。

可选环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `ROCEOS_DASH_PORT` | 9090 | 监听端口，被占用自动顺延 |
| `ROCEOS_DASH_IDLE_AFTER` | 45 | 无请求多少秒后进入省电 |
| `ROCEOS_DASH_IDLE_TICK` | 8 | 省电时的采样间隔（秒） |
| `ROCEOS_DASH_SNIFF` | 0 | 设为 1 才启用抓包（仅供旧版每设备速率，现已默认关闭） |

## 兼容性

- 需要：**Python 3.6+**、systemd（没有也能跑，退化成后台进程）
- 已在 iNextOS（Debian 13 trixie）R822 与 C4R4 两台机型上实测；
  Debian / Ubuntu / 其它 systemd 发行版同样适用
- 不是 iNextOS 也能用：找不到 roceos 数据库或 dnsmasq 租约时会自动降级

## 文件

- `dash.py` —— 全部代码（后端采集 + 前端页面），单文件
- `install.sh` —— 自包含一键安装脚本（内嵌 dash.py，base64 编码写入，避免 heredoc 被源码内容截断）

## 自适应与响应式

- 手机竖屏：单列，仪表盘上下排列
- 手机横屏 / 矮屏：压缩间距与图表高度
- 平板：两列，CPU/内存模块占满整行
- 显示器（≥1500px）：四列栅格，最大宽度 1680px
