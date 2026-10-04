# 路由看板 · iNextOS Router Dashboard

一个**单文件、零依赖**的路由器实时监控看板：只用 Python3 标准库，不需要 pip 安装任何东西。
跑在路由器上，局域网内任何设备打开 `http://<路由IP>:9090` 即可查看，**3 秒自动刷新**。

## 一键部署（在任何 iNextOS / Debian / Ubuntu 路由上）

```bash
curl -fsSL https://raw.githubusercontent.com/a6726170/roceos-dash/main/install.sh | bash
```

备用地址（国内网络 / GitHub 不通时）：

```bash
# jsDelivr CDN 镜像
curl -fsSL https://cdn.jsdelivr.net/gh/a6726170/roceos-dash@main/install.sh | bash

# 直连镜像站
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

- **设备负载**：CPU 环形仪表 + 每核占用、内存/交换、1/5/15 分钟负载曲线、CPU 温度、进程数、连接跟踪数
- **网口数据**：只显示**物理网卡**（虚拟网卡如 docker0/virbr0 只在真正工作时才出现），
  每个口给出实时下行/上行速率 + 趋势折线、累计流量、MAC、IP、速率档位、MTU、包数、错误数
- **在线设备**：主机名 / IP / **实时 ↓↑ 速率** / 今日流量 / MAC / 厂商 / 接口 / 在线状态 / 在线时长
- **广域网 WAN**：接入类型、连接状态、**WAN 口 IP**、**DNS 列表（含来源文件）**、网关、MTU、拨号时长、累计与实时流量
- **服务状态**：只列出本机真实存在的服务单元

## 数据来源

| 数据 | 来源 |
|---|---|
| CPU / 内存 / 负载 / 温度 | `/proc/stat`、`/proc/meminfo`、`/proc/loadavg`、`/sys/class/thermal` |
| 网口流量 | `/sys/class/net/*/statistics` |
| 设备清单 | dnsmasq 租约 + ARP 邻居表 + roceos 数据库（有哪个用哪个） |
| **设备实时速率** | 以太网口抓包按 MAC 聚合（默认）；内核 conntrack 计费可用时用 conntrack；都没有时用本小时均值估算 |
| 今日流量 | roceos `device_traffic_hourly` |
| WAN | `ip route show default` + `/etc/resolv.conf`（或 PPP 下发的） |

> 为什么不用 conntrack 做实时速率？因为这代 iNextOS 开了 nftables `flowtable`（flow offload），
> 卸载的流量不再更新 conntrack 字节计数，所以用抓包统计更准。抓包线程有 CPU 保护
> （单窗口最多处理 20000 包），实测占用约 1% CPU、16~25 MB 内存。

## 兼容性

- 需要：**Python 3.6+**、root 权限（抓包需要）、systemd（没有也能跑，退化成后台进程）
- 已在 iNextOS（Debian 13 trixie）上实测；Debian / Ubuntu / 其它 systemd 发行版同样适用
- 不是 iNextOS 也能用：找不到 roceos 数据库或 dnsmasq 租约时会自动降级，页脚会写明当前数据源

## 文件

- `dash.py` —— 全部代码（后端采集 + 前端页面），单文件
- `install.sh` —— 自包含一键安装脚本（内嵌 dash.py，base64 编码写入，避免 heredoc 被源码内容截断）

## 自适应与响应式

- 手机竖屏：单列 + 设备表格转为卡片
- 手机横屏 / 矮屏：压缩间距与图表高度
- 平板：两列
- 显示器（≥1500px）：四列栅格，最大宽度 1680px
