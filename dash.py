#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
iNextOS / Debian 路由看板  ——  单文件、零依赖（仅 Python3 标准库）
提供 :9090 的 Web 界面与 /api/stats JSON 接口，前端 3 秒刷新。
"""
import os
import re
import io
import sys
import time
import json
import glob
import socket
import struct
import sqlite3
import select
import threading
import subprocess
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

PORT = int(os.environ.get("ROCEOS_DASH_PORT", "9090"))
ACTUAL_PORT = PORT          # 端口被占用时自动向后顺延，实际端口在这里
ENV = dict(os.environ, PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
           LC_ALL="C")
# ---- 省电策略：没人打开看板页面时，后台采样自动降频 ----
IDLE_AFTER = float(os.environ.get("ROCEOS_DASH_IDLE_AFTER", "45"))    # 无请求多久算空闲
IDLE_INTERVAL = float(os.environ.get("ROCEOS_DASH_IDLE_TICK", "8"))   # 空闲时的采样间隔
ACTIVE_INTERVAL = 1.0                                                 # 有人看时的采样间隔
LAST_REQ = [time.time()]          # 最后一次 HTTP 请求时间
WAKE = threading.Event()          # 有人访问时立刻唤醒采样线程

SKIP_IFACES = {"lo"}

# ---- 自适应路径探测：不同机型 / 版本上这些文件位置可能不同，找不到就优雅降级 ----
DB_GLOBS = [
    os.environ.get("ROCEOS_DB", ""),
    "/opt/roceos/data/roceos.db", "/opt/roceos/roceos.db",
    "/opt/roceos/data/*.db", "/opt/roceos/*.db", "/var/lib/roceos/*.db",
]
LEASE_GLOBS = [
    os.environ.get("DNSMASQ_LEASES", ""),
    "/var/lib/misc/dnsmasq.leases", "/var/lib/dnsmasq/dnsmasq.leases",
    "/var/db/dnsmasq.leases", "/run/dnsmasq.leases", "/var/lib/dnsmasq.leases",
]
_db_cache = {"path": None, "ts": 0.0}
_lease_cache = {"path": None, "ts": 0.0}


def _first_exist(patterns, cache):
    """按候选列表找第一个真实存在且非空的文件，结果缓存 60 秒。"""
    p = cache.get("path")
    if p and os.path.exists(p):
        return p
    if time.time() - cache.get("ts", 0.0) < 60:
        return p
    cache["ts"] = time.time()
    for pat in patterns:
        if not pat:
            continue
        for m in sorted(glob.glob(pat)):
            if os.path.isfile(m) and os.path.getsize(m) > 0:
                cache["path"] = m
                return m
    cache["path"] = None
    return None


def db_path():
    return _first_exist(DB_GLOBS, _db_cache)


def lease_file():
    return _first_exist(LEASE_GLOBS, _lease_cache)


# ---------- 工具 ----------
def read(path, default=""):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return default

def sh(cmd, timeout=5):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, env=ENV)
        return p.stdout
    except Exception:
        return ""

def sh_cached(cmd, cache, ttl, timeout=5):
    """带 TTL 的子进程结果缓存——弱 CPU 路由上每次 fork/exec 都是实打实的开销。"""
    now = time.time()
    if cache[0] and now - cache[0] < ttl:
        return cache[1]
    cache[0], cache[1] = now, sh(cmd, timeout=timeout)
    return cache[1]

def proc_route_default():
    """默认路由 (dev, gateway)：直接读 /proc/net/route，替代 `ip route show default`。

    /proc/net/route 的网关是十六进制小端 IPv4，默认路由的目的地址是 00000000。
    """
    try:
        with open("/proc/net/route", "r", errors="ignore") as f:
            f.readline()
            for line in f:
                p = line.split()
                if len(p) < 3 or p[1] != "00000000":
                    continue
                gw = ""
                try:
                    g = int(p[2], 16)
                    if g:
                        gw = socket.inet_ntoa(struct.pack("<I", g))
                except Exception:
                    pass
                return p[0], gw
    except Exception:
        pass
    return "", ""

def proc_arp():
    """ARP/邻居表 [(ip, mac, iface, 条目有效)]，替代 `ip neigh show`（零 fork）。

    列顺序：IP address | HW type | Flags | HW address | Mask | Device
    Flags 里 0x2 (ATF_COM) 表示条目已完成解析，即可认为这台设备在线。
    """
    out = []
    try:
        with open("/proc/net/arp", "r", errors="ignore") as f:
            f.readline()
            for line in f:
                p = line.split()
                if len(p) < 6:
                    continue
                ip, flags, mac, iface = p[0], p[2], p[3], p[5]
                if mac in ("00:00:00:00:00:00", "*") or ":" not in mac:
                    continue
                try:
                    ok = bool(int(flags, 16) & 0x2)          # ATF_COM：条目有效
                except Exception:
                    ok = False
                out.append((ip, mac.lower(), iface, ok))
    except Exception:
        pass
    return out

def proc_uptime_of(pattern, dev=None, boot=None, hz=None):
    """扫 /proc 找某个命令行含 pattern 的进程已运行秒数，替代 `ps | grep`。"""
    try:
        if boot is None:
            boot = time.time() - float(read("/proc/uptime", "0").split()[0] or 0)
        if hz is None:
            hz = float(os.sysconf("SC_CLK_TCK"))
        now = time.time()
        best = None
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
            except Exception:
                continue
            if pattern not in cmd:
                continue
            if dev and dev not in cmd:
                continue
            try:
                st = read("/proc/%s/stat" % pid)
                fields = st[st.rindex(")") + 2:].split()
                start = int(fields[19]) / hz            # starttime（第 22 字段）
            except Exception:
                continue
            age = now - (boot + start)
            if best is None or age > best:
                best = age
        return int(best) if best else None
    except Exception:
        return None

_CPU_MODEL = ["", False]

def cpu_model():
    """CPU 型号：进程内只读一次 /proc/cpuinfo（原先每次请求都 fork grep|cut）。"""
    if _CPU_MODEL[1]:
        return _CPU_MODEL[0]
    m = ""
    try:
        with open("/proc/cpuinfo", "r", errors="ignore") as f:
            for line in f:
                if line.startswith("model name"):
                    m = line.split(":", 1)[1].strip()
                    break
                if line.startswith("Hardware") and not m:
                    m = line.split(":", 1)[1].strip()[:40]
    except Exception:
        pass
    _CPU_MODEL[0], _CPU_MODEL[1] = m, True
    return m

def fmt_bytes(n):
    n = float(n or 0)
    for u in ("B", "K", "M", "G", "T"):
        if n < 1024 or u == "T":
            return "%.1f%s" % (n, u) if u != "B" else "%dB" % n
        n /= 1024.0
    return "%.1fT" % n

def fmt_rate(bps):
    return "%s/s" % fmt_bytes(bps)

def fmt_dur(sec):
    sec = int(sec or 0)
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m, s = divmod(sec, 60)
    if d:
        return "%d天%d小时" % (d, h)
    if h:
        return "%d小时%d分" % (h, m)
    return "%d分%d秒" % (m, s)

def parse_dt(s):
    if not s:
        return None
    s = s.strip().replace("T", " ")
    m = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})(\.\d+)?([+-]\d{2}:?\d{2}|Z)?", s)
    if not m:
        return None
    frac = (m.group(2) or ".0")[:7]
    tz = m.group(3) or ""
    if tz == "Z":
        tz = "+00:00"
    try:
        return datetime.fromisoformat(m.group(1) + frac + tz)
    except Exception:
        try:
            return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

OUI = {
    "00:e0:4c": "Realtek", "00:0e:2b": "研华/工控", "6c:4b:90": "Intel",
    "00:00:00": "未烧录", "c8:1f:66": "Intel",
    "18:65:90": "Apple", "f0:18:98": "Apple", "a4:83:e7": "Apple", "3c:15:c2": "Apple",
    "00:1a:11": "Google", "94:eb:2c": "Google", "20:df:b9": "Google",
    "b8:27:eb": "树莓派", "dc:a6:32": "树莓派", "e4:5f:01": "树莓派",
    "00:23:12": "TP-Link", "50:c7:bf": "TP-Link", "98:da:c4": "TP-Link", "b0:4e:26": "TP-Link",
    "00:0c:29": "VMware", "00:50:56": "VMware", "00:1c:42": "Parallels",
    "08:00:27": "VirtualBox", "0a:00:27": "VirtualBox",
    "00:e0:fc": "华为", "00:9a:cd": "华为", "04:9f:ca": "华为", "70:8a:09": "华为",
    "28:6e:d4": "荣耀", "ac:e2:d3": "荣耀",
    "64:cc:2e": "小米", "78:11:dc": "小米", "8c:de:f9": "小米", "50:64:2b": "小米",
    "f8:a4:5f": "小米", "0c:1d:af": "小米", "98:fa:e3": "小米",
    "00:26:5a": "中兴", "00:1f:ce": "中兴",
    "00:1b:44": "NETGEAR", "20:e5:2a": "NETGEAR", "a0:40:a0": "NETGEAR",
    "00:1d:7e": "Cisco", "00:24:c4": "Cisco",
    "00:12:17": "D-Link", "1c:af:f7": "D-Link",
    "f4:6d:04": "三星", "08:d4:0c": "三星", "5c:0a:5b": "三星", "a8:16:d1": "三星",
    "00:23:76": "HTC", "40:4e:36": "OPPO", "ec:5c:68": "OPPO",
    "00:9e:8e": "vivo", "6c:96:cf": "vivo",
    "f0:43:47": "一加", "94:87:e0": "Intel", "08:97:98": "Intel",
    "00:d8:61": "H3C", "5c:dd:70": "H3C",
    "00:03:7f": "海康威视", "c0:5b:76": "海康威视", "44:19:b6": "海康威视",
    "00:12:1c": "大华", "3c:ef:8c": "大华", "e0:50:8b": "大华",
    "00:0a:eb": "TP-Link", "d8:0d:17": "TP-Link",
    "00:15:5d": "Hyper-V", "52:54:00": "QEMU/KVM",
    "02:42:ac": "Docker", "36:67:ac": "Docker",
}

def vendor_of(mac):
    if not mac:
        return ""
    key = mac.lower()[:8]
    return OUI.get(key, "")

CT_FILE = "/proc/net/nf_conntrack"
CT_LINE = re.compile(r"src=(\S+)\s+dst=(\S+)\s+sport=\S+\s+dport=\S+\s+packets=\d+\s+bytes=(\d+)")


def is_private(ip):
    if ip.startswith("10.") or ip.startswith("192.168."):
        return True
    if ip.startswith("172."):
        try:
            return 16 <= int(ip.split(".")[1]) <= 31
        except Exception:
            return False
    return False


def conntrack_bytes():
    """按 IP 聚合连接跟踪的字节数 → {ip: (rx, tx)}，用于算每台设备的实时速率。
    依赖内核 conntrack accounting（多数发行版默认开）；不可用时返回 None。"""
    try:
        if os.path.getsize(CT_FILE) > 24 * 1024 * 1024:   # 连接表过大就不折腾了
            return None
        with open(CT_FILE, "r", errors="ignore") as f:
            buf = f.read()          # 整块读 + 一次 finditer：比逐行 findall 少上千次函数调用
    except Exception:
        return None
    rx, tx = {}, {}
    hit = False
    try:
        for m in CT_LINE.finditer(buf):
            hit = True
            s, d, b = m.group(1), m.group(2), int(m.group(3))
            if is_private(s):
                tx[s] = tx.get(s, 0) + b          # 该 IP 发出
            if is_private(d):
                rx[d] = rx.get(d, 0) + b          # 该 IP 收到
    except Exception:
        return None
    if not hit:
        return None
    return {ip: (rx.get(ip, 0), tx.get(ip, 0)) for ip in set(rx) | set(tx)}


# ---------- 采样线程（1 秒一次，算速率与 CPU 占用）----------
class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.cpu_hist = []          # [(ts, total, idle)]
        self.core_hist = []         # [(ts, [(total,idle), ...])]
        self.net_hist = {}          # iface -> [(ts, rx, tx)]
        self.ct_hist = []           # [(ts, {ip: (rx, tx)})] —— 每设备实时速率来源
        self.ct_ok = False          # conntrack accounting 是否可用
        self.last_net = {}          # 最近一次 /proc/net/dev 快照，供请求处理复用
        self.ct_live = False        # conntrack 字节数是否真的在变（被 flow offload 绕过时会一直是 False）
        self._ct_prev = None
        self._ct_ts = 0.0
        self._tick = 0
        self._last_ts = 0.0
        self.history = {
            "cpu": [], "load": [], "mem": [],
            "rx": {}, "tx": {},
        }
        self.maxlen = 120

    @staticmethod
    def cpu_stat():
        total = idle = 0
        cores = []
        with open("/proc/stat", "r") as f:
            for line in f:
                p = line.split()
                if p[0] == "cpu":
                    v = [int(x) for x in p[1:8]]
                    total = sum(v)
                    idle = v[3] + v[4]
                elif p[0].startswith("cpu"):
                    v = [int(x) for x in p[1:8]]
                    cores.append((sum(v), v[3] + v[4]))
        return total, idle, cores

    @staticmethod
    def net_stat():
        """一次 read 拿到全部网口收发字节（/proc/net/dev）。

        原来逐个读 /sys/class/net/*/statistics/{rx,tx}_bytes：17 个网口就是每秒 34 次
        open/read，在弱 CPU 上这部分占了后台开销的大头。
        """
        out = {}
        try:
            with open("/proc/net/dev", "r", errors="ignore") as f:
                f.readline()
                f.readline()
                for line in f:
                    p = line.split()
                    if len(p) < 13:
                        continue
                    try:
                        # rx_bytes, tx_bytes, rx_pkts, tx_pkts, rx_err, tx_err
                        out[p[0].rstrip(":")] = (int(p[1]), int(p[9]), int(p[2]),
                                                 int(p[10]), int(p[3]), int(p[11]))
                    except Exception:
                        continue
        except Exception:
            pass
        return out

    def dev_stats(self, iface):
        """最近一次采样的网口累计量（rx, tx, rx_pkts, tx_pkts, rx_err, tx_err）。

        请求处理直接复用采样线程的快照，避免再逐个读 /sys/class/net/*/statistics/*。
        """
        with self.lock:
            v = self.last_net.get(iface)
        return v or (0, 0, 0, 0, 0, 0)

    def idle(self):
        """没人看页面 → True（进入省电：拉长间隔、不解析 conntrack）。"""
        return (time.time() - LAST_REQ[0]) > IDLE_AFTER

    def run(self):
        while True:
            WAKE.clear()
            now = time.time()
            # 停顿太久（省电恢复/机器休眠）后，旧采样点不能拿来差分，直接丢弃
            if self._last_ts and now - self._last_ts > max(6.0, IDLE_INTERVAL * 1.5):
                with self.lock:
                    self.cpu_hist.clear()
                    self.core_hist.clear()
                    self.net_hist.clear()
                    self.ct_hist.clear()
                self._ct_prev = None
            self._last_ts = now
            total, idle, cores = self.cpu_stat()
            net = self.net_stat()
            self._tick += 1
            idle_now = self.idle()
            # 活跃时每 2 秒采一次；刚从省电恢复（或首次）立即补采一次，避免页面
            # 刚打开时设备速率全是 0
            if not idle_now and (self._tick % 2 == 0 or now - self._ct_ts > 5.0):
                self._ct_ts = now
                ct = conntrack_bytes()
                if ct is not None:
                    self.ct_ok = True
                    # 只对"两次快照都在"的 IP 求字节差：连接的新建/销毁会让总量变化，
                    # 但被 flow offload 绕过的老连接字节数根本不动，这样能准确识别出来
                    prev = self._ct_prev
                    if prev is not None:
                        moved = 0
                        for ip, v in ct.items():
                            p = prev.get(ip)
                            if p:
                                moved += abs(v[0] - p[0]) + abs(v[1] - p[1])
                        self.ct_live = bool(moved)
                    self._ct_prev = ct
                    with self.lock:
                        self.ct_hist.append((now, ct))
                        self.ct_hist = self.ct_hist[-4:]
            with self.lock:
                self.cpu_hist.append((now, total, idle))
                self.cpu_hist = self.cpu_hist[-5:]
                self.core_hist.append((now, cores))
                self.core_hist = self.core_hist[-5:]
                for iface, v in net.items():
                    h = self.net_hist.setdefault(iface, [])
                    h.append((now, v[0], v[1]))
                    self.net_hist[iface] = h[-5:]
            with self.lock:
                self.last_net = net
            # 有人访问就 1 秒一次；没人看就降到 IDLE_INTERVAL（页面关掉后几乎不吃 CPU）。
            # wait 可被有人打开页面时的 wake() 打断，做到"一访问立刻恢复精度"。
            WAKE.wait(IDLE_INTERVAL if idle_now else ACTIVE_INTERVAL)

    def cpu_usage(self):
        with self.lock:
            h = list(self.cpu_hist)
        if len(h) < 2:
            return 0.0
        (t0, a0, i0), (t1, a1, i1) = h[0], h[-1]
        dt, di = a1 - a0, i1 - i0
        if dt <= 0:
            return 0.0
        return round(max(0.0, min(100.0, (dt - di) * 100.0 / dt)), 1)

    def core_usage(self):
        with self.lock:
            h = list(self.core_hist)
        if len(h) < 2:
            return []
        (_, c0), (_, c1) = h[0], h[-1]
        out = []
        for (t0, i0), (t1, i1) in zip(c0, c1):
            dt, di = t1 - t0, i1 - i0
            out.append(round(max(0.0, min(100.0, (dt - di) * 100.0 / dt)), 1) if dt > 0 else 0.0)
        return out

    def dev_rate(self, ip):
        """某台设备的实时上下行速率 (rx_Bps, tx_Bps)；数据不足返回 None。"""
        if not ip:
            return None
        with self.lock:
            h = list(self.ct_hist)
        if len(h) < 2:
            return None
        (t0, d0), (t1, d1) = h[0], h[-1]
        dt = t1 - t0
        if dt <= 0 or dt > 15:      # 间隔过大（省电刚恢复）时差值没有意义，宁可不显示
            return None
        r0, x0 = d0.get(ip, (0, 0))
        r1, x1 = d1.get(ip, (0, 0))
        return (max(0.0, (r1 - r0) / dt), max(0.0, (x1 - x0) / dt))

    def net_rate(self, iface):
        with self.lock:
            h = list(self.net_hist.get(iface, []))
        if len(h) < 2:
            return 0.0, 0.0
        (t0, r0, x0), (t1, r1, x1) = h[0], h[-1]
        dt = t1 - t0
        if dt <= 0:
            return 0.0, 0.0
        return max(0.0, (r1 - r0) / dt), max(0.0, (x1 - x0) / dt)

SAMPLER = Sampler()


# ---------- 抓包采样线程：统计每台设备的实时上下行 ----------
# 说明：本机开了 nftables flowtable(flow offload)，nf_conntrack 的 bytes 不再更新，
#      因此不能用 conntrack 计费，改为在以太网物理口上抓包按 MAC 聚合（零依赖、无需 tcpdump）。
class Sniffer(threading.Thread):
    ETH_P_ALL = 3
    WINDOW = 0.5            # 本地累计合并周期
    MAX_PKT = 20000         # 单窗口最多处理的包数（保护 CPU，超出即降采样）
    BUFSIZE = 2048          # 关键：标准以太帧 <=1514 字节，用 65536 会让每个包都分配 64KB，
                            # 在千包/秒的场景下光内存拷贝就吃掉十几个点 CPU
    IDLE_ROUNDS = 2         # 连续几次巡检都零流量就跳过该口（省掉整条抓包链路）

    def __init__(self):
        super().__init__(daemon=True)
        self.lock = threading.Lock()
        self.acc = {}                  # mac(bytes) -> [up, down]
        self.rate = {}                 # mac(str)   -> [rx_bps, tx_bps]
        self.last_active = {}
        self.last_tick = time.time()
        self.saturated = False
        self.active = False
        self.ifaces = []
        self.self_macs = set()
        self.workers = []
        self._idle = {}             # iface -> 连续零流量次数
        self._prev_pkts = {}

    @staticmethod
    def _master(iface):
        """网卡若已加入网桥，返回网桥名（抓桥就能看到所有成员口的流量，避免重复抓）"""
        for sub in ("master", os.path.join("brport", "bridge")):
            p = os.path.join("/sys/class/net", iface, sub)
            try:
                return os.path.basename(os.readlink(p))
            except Exception:
                continue
        return None

    @staticmethod
    def _pkts(iface):
        base = os.path.join("/sys/class/net", iface, "statistics")
        try:
            return (int(read(os.path.join(base, "rx_packets"), "0") or 0)
                    + int(read(os.path.join(base, "tx_packets"), "0") or 0))
        except Exception:
            return 0

    # --- 选接口：up 且有 MAC 的以太网物理口 ---
    def pick_ifaces(self):
        phys, virt = [], []
        for i in sorted(os.listdir("/sys/class/net")):
            if i == "lo":
                continue
            base = "/sys/class/net/" + i
            if read(os.path.join(base, "operstate")) not in ("up", "unknown"):
                continue
            mac = read(os.path.join(base, "address"))
            if not mac or mac == "00:00:00:00:00:00":
                continue
            try:
                mb = bytes(int(x, 16) for x in mac.split(":"))
            except Exception:
                continue
            self.self_macs.add(mb)
            (phys if os.path.exists(os.path.join(base, "device")) else virt).append(i)

        # 网桥成员口 → 改抓网桥本身（一个桥覆盖所有成员口，省掉 N 条抓包链路）
        targets, seen = [], set()
        for i in phys:
            t = self._master(i) or i
            if t not in seen:
                seen.add(t)
                targets.append(t)

        # 零流量的口整条链路停掉（计数来自 sysfs，有流量时下个周期会自动恢复）
        cur = {i: self._pkts(i) for i in targets}
        active = []
        for i in targets:
            prev = self._prev_pkts.get(i)
            if prev is not None and cur[i] == prev:
                self._idle[i] = self._idle.get(i, 0) + 1
            else:
                self._idle[i] = 0
            if self._idle.get(i, 0) < self.IDLE_ROUNDS:
                active.append(i)
        self._prev_pkts = cur
        return active

    def _open(self, iface):
        try:
            s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW,
                              socket.htons(self.ETH_P_ALL))
            s.bind((iface, 0))
            s.setblocking(False)
            return s
        except Exception:
            return None

    def _worker(self, iface, sock):
        local = {}
        last = time.time()
        while True:
            try:
                r, _, _ = select.select([sock], [], [], self.WINDOW)
            except Exception:
                break
            if r:
                n = 0
                while n < self.MAX_PKT:
                    try:
                        data = sock.recv(self.BUFSIZE)
                    except BlockingIOError:
                        break
                    except Exception:
                        return
                    self._feed(data, local)
                    n += 1
                if n >= self.MAX_PKT:
                    self.saturated = True
            now = time.time()
            if now - last >= self.WINDOW:
                if local:
                    with self.lock:
                        for m, (u, d) in local.items():
                            a = self.acc.setdefault(m, [0, 0])
                            a[0] += u
                            a[1] += d
                    local.clear()
                last = now

    def _feed(self, data, local):
        n = len(data)
        if n < 14:
            return
        d12 = data[12]                                   # 只统计 IPv4(0x0800) / IPv6(0x86dd)
        if d12 == 8:
            if data[13]:
                return
        elif d12 == 0x86:
            if data[13] != 0xDD:
                return
        else:
            return
        src = data[6:12]
        dst = data[0:6]
        mine = self.self_macs
        if src in mine:                                  # 网关发出 → 设备下行
            if dst[0] & 1:                               # 广播/组播不算到单个设备
                return
            key, idx = dst, 1
        elif dst in mine:                                # 设备发给网关 → 上行
            if src[0] & 1:
                return
            key, idx = src, 0
        else:                                            # 局域网内互访，不经过网关，忽略
            return
        a = local.get(key)
        if a is None:
            a = local[key] = [0, 0]
        a[idx] += n

    def run(self):
        while True:
            # conntrack 字节数是活的，就完全不抓包（抓包在千包/秒的场景要十几个点 CPU，
            # conntrack 读一遍只要几毫秒）；哪天 conntrack 被 flow offload 绕过不动了，
            # 下个周期会自动恢复抓包。没人看页面时也一并停掉。
            want = []
            if not SAMPLER.ct_live and (time.time() - LAST_REQ[0]) <= IDLE_AFTER:
                want = self.pick_ifaces()
            have = set(self.ifaces)
            if want != self.ifaces:
                self.ifaces = want
                for s in self.workers:
                    try:
                        s.close()
                    except Exception:
                        pass
                self.workers = []
                for iface in want:
                    s = self._open(iface)
                    if s is None:
                        continue
                    self.workers.append(s)
                    threading.Thread(target=self._worker, args=(iface, s),
                                     daemon=True).start()
                self.active = bool(self.workers)
            for _ in range(15):        # 巡检周期 15 秒；期间有人访问就提前重算
                time.sleep(1)
                if (time.time() - LAST_REQ[0]) <= IDLE_AFTER:
                    break

    def tick(self):
        """每 3 秒结算一次速率：设备视角 rx=下行(下载)、tx=上行(上传)"""
        now = time.time()
        with self.lock:
            snap, self.acc = self.acc, {}
        dt = max(0.5, now - self.last_tick)
        self.last_tick = now
        seen = []
        for m, (up, down) in snap.items():
            key = ":".join("%02x" % b for b in m)
            seen.append(key)
            ru = up / dt        # 设备上传
            rd = down / dt      # 设备下载
            old = self.rate.get(key)
            if old:
                ru = ru * 0.5 + old[1] * 0.5
                rd = rd * 0.5 + old[0] * 0.5
            self.rate[key] = [rd, ru]
        for k in seen:
            self.last_active[k] = now
        for k in [k for k, t in self.last_active.items() if now - t > 300]:
            self.rate.pop(k, None)          # 5 分钟没流量就丢弃，避免无限增长
            self.last_active.pop(k, None)
        return self.rate

    def rate_of(self, mac):
        return self.rate.get((mac or "").lower())


SNIFFER = Sniffer()

# ---------- 采集各项指标 ----------
def collect_system():
    load = read("/proc/loadavg").split()
    up = float(read("/proc/uptime", "0").split()[0] or 0)
    mi = {}
    for line in read("/proc/meminfo").splitlines():
        p = line.split()
        if len(p) >= 2:
            mi[p[0].rstrip(":")] = int(p[1])
    total = mi.get("MemTotal", 0)
    avail = mi.get("MemAvailable", 0)
    used = total - avail
    temp = None
    for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*/temp")):
        v = read(z)
        if v.isdigit():
            temp = int(v) / 1000.0
            break
    conn = read("/proc/sys/net/netfilter/nf_conntrack_count", "")
    conn_max = read("/proc/sys/net/netfilter/nf_conntrack_max", "")
    # 进程数：每 3 秒 listdir 一次 /proc 属于纯浪费，缓存 3 秒
    now = time.time()
    if now - _PROCS_CACHE[0] > 3.0:
        _PROCS_CACHE[0] = now
        _PROCS_CACHE[1] = len([d for d in os.listdir("/proc") if d.isdigit()])
    procs = _PROCS_CACHE[1]
    return {
        "hostname": socket.gethostname(),
        "kernel": read("/proc/sys/kernel/osrelease"),
        "cpu_model": cpu_model(),
        "cores": os.cpu_count() or 1,
        "uptime": up,
        "uptime_text": fmt_dur(up),
        "load": [float(x) for x in load[:3]] if load else [0, 0, 0],
        "procs": procs,
        "cpu": SAMPLER.cpu_usage(),
        "mem": {
            "total": total * 1024, "used": used * 1024,
            "free": avail * 1024,
            "cached": (mi.get("Cached", 0) + mi.get("Buffers", 0)) * 1024,
            "percent": round(used * 100.0 / total, 1) if total else 0,
            "swap_total": mi.get("SwapTotal", 0) * 1024,
            "swap_used": (mi.get("SwapTotal", 0) - mi.get("SwapFree", 0)) * 1024,
        },
        "temp": temp,
        "conntrack": int(conn) if conn.isdigit() else None,
        "conntrack_max": int(conn_max) if conn_max.isdigit() else None,
    }

def is_physical(iface):
    """有真实父设备（PCI/USB）或无线的才是物理网卡。docker0/virbr0/veth/ppp 都没有。"""
    base = "/sys/class/net/" + iface
    for sub in ("device", "phy80211", "wireless"):
        if os.path.exists(os.path.join(base, sub)):
            return True
    # 部分平台（如某些 SoC）不建 device 链接，用驱动目录排除已知虚拟类型
    drv = os.path.basename(os.path.realpath(os.path.join(base, "device", "driver"))) \
        if os.path.exists(os.path.join(base, "device")) else ""
    return bool(drv)


def iface_active(info, wan_dev=""):
    """虚拟网卡只有"真在工作"时才展示。

    规则：必须自己有 IP（说明确实在承载三层通信），并且有流量或链路。
    这样会自动隐藏：docker0（没起容器时零流量）、virbr0、没加入网络的 ZeroTier 口。
    ifb* 是 tc 流量整形的垫片设备，流量与真实口完全重复，直接排除。
    WAN 口无条件保留（哪怕瞬时没流量，也不该从页面消失）。
    """
    if info["name"].startswith("ifb"):
        return False
    if wan_dev and info["name"] == wan_dev:
        return True
    # 只有链路本地 IPv6（fe80::）不算"在用"——未加入网络的 ZeroTier / 空网桥都是这种
    l3 = bool(info["ip"]) or any(
        not a.lower().startswith("fe80:") for a in (info["ipv6"] or []))
    if not l3:
        return False
    return info["rx_rate"] + info["tx_rate"] > 1024 or info["carrier"]


_ADDR_CACHE = [0.0, ""]         # `ip -o addr show` 结果缓存 5 秒（地址不会每秒变）
_ROUTE_CACHE = [0.0, ""]        # `ip route show default` 缓存 10 秒（仅 /proc 失败时用）
_IFACE_STATIC = {}              # 网口不变的属性（MAC/速率/MTU/是否物理）：进程内只读一次
_IFACE_STATE = [0.0, {}]        # operstate/carrier 缓存 3 秒


def iface_addrs():
    """所有网口的 IP 地址 {iface: [addr, ...]}，缓存 5 秒。"""
    txt = sh_cached("ip -o addr show", _ADDR_CACHE, 5.0)
    addrs = {}
    for line in txt.splitlines():
        p = line.split()
        if len(p) >= 4 and p[2] in ("inet", "inet6"):
            addrs.setdefault(p[1], []).append(p[3])
    return addrs


def iface_static(iface, base):
    """MAC / permaddr / MTU / 速率 / 是否物理网卡——一辈子不变，只读一次。"""
    v = _IFACE_STATIC.get(iface)
    if v is None:
        try:
            speed = int(read(os.path.join(base, "speed"), "-1"))
        except Exception:
            speed = -1
        mac = read(os.path.join(base, "address"))
        v = {
            "mac": mac,
            "permaddr": read(os.path.join(base, "permaddr")),
            "vendor": vendor_of(mac),
            "speed": speed,
            "mtu": int(read(os.path.join(base, "mtu"), "0") or 0),
            "physical": is_physical(iface),
        }
        _IFACE_STATIC[iface] = v
    return v


def iface_state(iface, base):
    """链路状态（operstate / carrier）缓存 3 秒，避免每请求两次 sysfs 读。"""
    now = time.time()
    if now - _IFACE_STATE[0] > 3.0:
        _IFACE_STATE[0], _IFACE_STATE[1] = now, {}
    v = _IFACE_STATE[1].get(iface)
    if v is None:
        v = _IFACE_STATE[1][iface] = (
            read(os.path.join(base, "operstate")),
            read(os.path.join(base, "carrier"), "0") == "1",
        )
    return v


def collect_ifaces():
    out = {}
    addrs = iface_addrs()
    wd = detect_wan_dev()[0]
    for iface in sorted(os.listdir("/sys/class/net")):
        if iface in SKIP_IFACES:
            continue
        base = "/sys/class/net/" + iface
        rx, tx, rxp, txp, rxe, txe = SAMPLER.dev_stats(iface)
        rr, tr = SAMPLER.net_rate(iface)
        st = iface_static(iface, base)
        state, carrier = iface_state(iface, base)
        ip_list = addrs.get(iface, [])
        ip4 = [a.split("/")[0] for a in ip_list if ":" not in a]
        info = {
            "name": iface,
            "state": state,
            "carrier": carrier,
            "ip": ip4,
            "ipv6": [a.split("/")[0] for a in ip_list if ":" in a],
            "rx_bytes": rx, "tx_bytes": tx,
            "rx_rate": rr, "tx_rate": tr,
            "rx_text": fmt_bytes(rx), "tx_text": fmt_bytes(tx),
            "rx_rate_text": fmt_rate(rr), "tx_rate_text": fmt_rate(tr),
            "rx_packets": rxp, "tx_packets": txp,
            "rx_errors": rxe, "tx_errors": txe,
        }
        info.update(st)
        # 只展示物理网卡；虚拟网卡（docker/virbr/veth…）未工作时不显示
        if not info["physical"] and not iface_active(info, wd):
            continue
        out[iface] = info
    # 物理网卡排前面
    return dict(sorted(out.items(), key=lambda kv: (not kv[1]["physical"], kv[0])))


def detect_wan_dev():
    """按默认路由推断 WAN 口：优先读 /proc/net/route（零 fork），
    取不到再退回 `ip route`（缓存），最后才是常见名称兜底。"""
    dev, via = proc_route_default()
    if not dev:
        txt = sh_cached("ip route show default", _ROUTE_CACHE, 10.0)
        for line in txt.splitlines():
            m = re.search(r"\bdev\s+(\S+)", line)
            if m and not dev:
                dev = m.group(1)
                v = re.search(r"\bvia\s+(\S+)", line)
                via = v.group(1).split("/")[0] if v else ""
    if not dev:
        for cand in ("ppp0", "pppoe-wan", "wan", "eth1", "eth0"):
            if os.path.isdir("/sys/class/net/" + cand):
                dev = cand
                break
    return dev, via


_WAN_TYPE_CACHE = [0.0, {}]
_PROCS_CACHE = [0.0, 0]         # /proc 进程数：3 秒
_WAN_MISC_CACHE = [0.0, {}]     # WAN IP / peer：10 秒
_PPPD_CACHE = [0.0, None]       # pppd 已拨号秒数：30 秒

def wan_type(dev):
    """WAN 接入方式：一次判定缓存 5 分钟——原来每 3 秒就 fork 好几个
    systemctl/grep/ls，在弱 CPU 上是最贵的一笔固定开销。"""
    if not dev:
        return "--"
    now = time.time()
    if now - _WAN_TYPE_CACHE[0] > 300.0:
        _WAN_TYPE_CACHE[0], _WAN_TYPE_CACHE[1] = now, {}
    got = _WAN_TYPE_CACHE[1].get(dev)
    if got:
        return got
    t = _detect_wan_type(dev)
    _WAN_TYPE_CACHE[1][dev] = t
    return t


def _detect_wan_type(dev):
    if dev.startswith("ppp"):
        return "PPPoE 拨号"
    if sh("grep -rqs 'iface %s inet dhcp' /etc/network/interfaces" % dev).strip():
        return "DHCP 自动获取"
    for f in ("/etc/network/interfaces", "/etc/network/interfaces.d/%s" % dev):
        txt = read(f)
        if txt and dev in txt and "dhcp" in txt:
            return "DHCP 自动获取"
    if sh("systemctl is-enabled dhcpcd 2>/dev/null; systemctl is-enabled NetworkManager 2>/dev/null"):
        if "enabled" in sh("systemctl is-enabled dhcpcd 2>/dev/null"):
            return "DHCP 自动获取"
    if sh("ls /var/lib/dhcp/dhclient*%s* 2>/dev/null" % dev).strip():
        return "DHCP 自动获取"
    return "静态地址 / 其他"


def collect_wan(ifaces):
    dev, via = detect_wan_dev()
    it = ifaces.get(dev, {})
    ip = it.get("ip", [""])[0] if it.get("ip") else ""
    # DNS / PPPoE 拨号时长：都不需要子进程，且变化很慢，缓存 10 秒
    now = time.time()
    if now - _WAN_MISC_CACHE[0] > 10.0:
        _WAN_MISC_CACHE[0], _WAN_MISC_CACHE[1] = now, {}
    misc = _WAN_MISC_CACHE[1]
    if "ip" not in misc and not ip and dev:
        for line in sh("ip -o -4 addr show dev %s" % dev).splitlines():
            p = line.split()
            if len(p) >= 4 and p[2] == "inet":
                misc["ip"] = p[3].split("/")[0]
                break
        misc.setdefault("ip", "")
    ip = ip or misc.get("ip", "")
    peer = via
    if not peer:
        if "peer" not in misc:
            misc["peer"] = ""
            for line in sh("ip -o -4 addr show dev %s" % dev).splitlines():
                m = re.search(r"peer (\S+)", line)
                if m:
                    misc["peer"] = m.group(1).split("/")[0]
        peer = misc.get("peer", "")
    # DNS：优先 PPP 下发的，再取系统 resolv.conf
    dns, dns_src = [], ""
    for f in ("/etc/ppp/resolv.conf", "/run/ppp/resolv.conf", "/etc/resolv.conf"):
        got = []
        for line in read(f).splitlines():
            if line.startswith("nameserver"):
                parts = line.split()
                if len(parts) > 1 and parts[1] not in got:
                    got.append(parts[1])
        if got:
            dns, dns_src = got[:4], f
            break
    # PPPoE 拨号时长：扫 /proc 找 pppd（零子进程），缓存 30 秒
    since = None
    if dev.startswith("ppp"):
        if now - _PPPD_CACHE[0] > 30.0:
            _PPPD_CACHE[0] = now
            _PPPD_CACHE[1] = proc_uptime_of("pppd", dev)
            if _PPPD_CACHE[1] is None:
                _PPPD_CACHE[1] = proc_uptime_of("pppd")
        since = _PPPD_CACHE[1]
    return {
        "connected": bool(ip) and (it.get("state", "") != "down" or it.get("carrier")),
        "iface": dev or "--",
        "type": wan_type(dev) if dev else "--",
        "ip": ip,
        "peer": peer,
        "dns": dns,
        "dns_src": dns_src,
        "mtu": it.get("mtu"),
        "mac": it.get("mac", ""),
        "since": since,
        "uptime_text": fmt_dur(since) if since else "--",
        "rx_text": it.get("rx_text", "--"),
        "tx_text": it.get("tx_text", "--"),
        "rx_rate_text": it.get("rx_rate_text", "--"),
        "tx_rate_text": it.get("tx_rate_text", "--"),
    }

LEASE_RE = re.compile(r"^\d+\s+(\S+)\s+(\S+)\s+(\S*)")

def collect_devices():
    now = datetime.now().astimezone()
    devs = {}

    def put(mac, **kw):
        if not mac:
            return
        mac = mac.lower()
        d = devs.setdefault(mac, {"mac": mac, "ip": "", "hostname": "", "iface": "",
                                  "vendor": vendor_of(mac), "rx": 0, "tx": 0,
                                  "online": False, "since": None, "last_seen": None,
                                  "source": []})
        for k, v in kw.items():
            if v not in (None, "", []):
                d[k] = v

    # 1) dnsmasq 租约
    lf = lease_file()
    if lf:
        for line in read(lf).splitlines():
            m = LEASE_RE.match(line.strip())
            if m:
                put(m.group(1), ip=m.group(2), hostname=m.group(3) or "")
                devs[m.group(1).lower()]["source"].append("DHCP")

    # 2) ARP / 邻居表（真正的“在线”判定）——直接读 /proc/net/arp，零子进程
    for ip, mac, iface, ok in proc_arp():
        if ok:
            put(mac, ip=ip, iface=iface, online=True, last_seen=now.isoformat())
            devs[mac]["source"].append("ARP")

    # 3) roceos 在线会话（数据库/表不存在时静默跳过，保证非 iNextOS 也能跑）
    dbp = db_path()
    if dbp:
        con = None
        try:
            con = sqlite3.connect("file:%s?mode=ro" % dbp, uri=True, timeout=2)
            con.row_factory = sqlite3.Row
            tables = {r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if "device_online_sessions" in tables:
                for r in con.execute(
                        "SELECT mac,ip,hostname,iface,start_time,last_seen FROM device_online_sessions "
                        "WHERE end_time IS NULL OR end_time=''"):
                    mac = (r["mac"] or "").lower()
                    if not mac:
                        continue
                    put(mac, ip=r["ip"] or "", hostname=r["hostname"] or "",
                        iface=r["iface"] or "")
                    d = devs[mac]
                    d["source"].append("roceos")
                    st = parse_dt(r["start_time"])
                    if st:
                        d["since"] = st.isoformat()
                    ls = parse_dt(r["last_seen"])
                    if ls:
                        d["_r_last"] = ls
                        d["_r_open"] = True
            # 4) 今日流量
            today = time.strftime("%Y-%m-%d")
            if "device_traffic_hourly" in tables:
                for r in con.execute(
                        "SELECT mac, SUM(rx_bytes) rx, SUM(tx_bytes) tx FROM device_traffic_hourly "
                        "WHERE substr(hour_start,1,10)=? GROUP BY mac", (today,)):
                    mac = (r["mac"] or "").lower()
                    if mac in devs:
                        devs[mac]["rx"] = r["rx"] or 0
                        devs[mac]["tx"] = r["tx"] or 0
                # 当前这一小时的流量：conntrack 不可用时用来估算速率
                hour = time.strftime("%Y-%m-%d %H")
                mins = datetime.now().minute + 1
                for r in con.execute(
                        "SELECT mac, SUM(rx_bytes) rx, SUM(tx_bytes) tx FROM device_traffic_hourly "
                        "WHERE substr(hour_start,1,13)=? GROUP BY mac", (hour,)):
                    mac = (r["mac"] or "").lower()
                    if mac in devs:
                        devs[mac]["_hr"] = (r["rx"] or 0, r["tx"] or 0, mins)
        except Exception:
            pass
        finally:
            try:
                if con:
                    con.close()
            except Exception:
                pass

    for d in devs.values():
        d["source"] = sorted(set(d.pop("source", [])))
        d["rx_text"] = fmt_bytes(d["rx"])
        d["tx_text"] = fmt_bytes(d["tx"])
        d["vendor"] = vendor_of(d["mac"])
        if d.get("since"):
            st = parse_dt(d["since"])
            d["online_text"] = fmt_dur((now - st).total_seconds()) if st else "--"
        else:
            d["online_text"] = "--"
        if not d["hostname"]:
            d["hostname"] = "未知设备"
        # 在线判定：ARP 表里有有效条目  或  roceos 会话未结束且 10 分钟内还见过
        arp_on = bool(d.get("online"))
        r_last = d.get("_r_last")
        sess_on = bool(d.get("_r_open") and r_last
                       and (now - r_last).total_seconds() < 600)
        d["online"] = bool(arp_on or sess_on)
        d.pop("_arp", None); d.pop("_r_last", None); d.pop("_r_open", None)

        # 实时速率（设备视角 rx=下载 / tx=上传），三级数据源：
        #   ① 抓包采样（最准，覆盖所有经网关的流量，不受 flow offload 影响）
        #   ② conntrack 按 IP 字节差（内核 accounting 可用时才有效）
        #   ③ roceos 本小时均值（兜底粗算，标 ~）
        rate, src = None, ""
        rt = SNIFFER.rate_of(d["mac"])
        if rt and (rt[0] + rt[1]) > 0:
            rate, src = (rt[0], rt[1]), "sniff"
        if rate is None:
            rate = SAMPLER.dev_rate(d.get("ip"))
            if rate:
                src = "ct"
        hr = d.pop("_hr", None)
        if rate is None and hr and hr[2] > 0:
            rate = (hr[0] / (hr[2] * 60.0), hr[1] / (hr[2] * 60.0))
            src = "hour"
        if rate:
            d["rx_rate"], d["tx_rate"] = round(rate[0], 1), round(rate[1], 1)
            d["rx_rate_text"], d["tx_rate_text"] = fmt_rate(rate[0]), fmt_rate(rate[1])
        else:
            d["rx_rate"], d["tx_rate"] = 0.0, 0.0
            d["rx_rate_text"], d["tx_rate_text"] = "0B/s", "0B/s"
        d["rate_src"] = src
        d["approx"] = (src == "hour")
        # 有实时流量也说明设备在线（ARP 已老化、roceos 没记录时靠它兜底）
        flow_on = bool(rate and (rate[0] + rate[1]) > 2048)
        d["online"] = bool(d["online"] or flow_on)
    return sorted(devs.values(),
                  key=lambda x: (not x["online"], -(x["rx_rate"] + x["tx_rate"]),
                                 -(x["rx"] + x["tx"])))

WATCH_SERVICES = ["roceos", "roceos-guard", "roceos-vision", "nginx", "dnsmasq", "ssh",
                  "smbd", "nmbd", "vsftpd", "netatalk", "docker", "libvirtd",
                  "fail2ban", "redis-server", "nfs-server", "ovs-vswitchd"]
SYS_NOISE = ("systemd-", "dbus", "getty", "user@", "session-", "udisks2", "polkit",
             "colord", "cron", "rsyslog", "ModemManager", "wpa_supplicant", "avahi-daemon")


_SVC_CACHE = [0.0, []]      # [时间戳, 服务列表]   —— 5 秒
_UF_CACHE = [0.0, {}]       # [时间戳, unit 文件表] —— 300 秒


def collect_services():
    """只显示本机真实存在的服务（非 iNextOS / Debian 通用机上也不会出现一堆灰灯）。

    性能：systemctl list-unit-files 在弱 CPU（如 J4125）上要 1~1.5 秒，而页面每 3 秒
    刷一次，逐个请求重跑会白白吃掉半个核。unit 文件清单几乎不变 → 缓存 5 分钟；
    运行中的服务列表缓存 15 秒（服务启停不至于要秒级可见，多标签页也只算一次）。
    """
    now = time.time()
    if _SVC_CACHE[0] and now - _SVC_CACHE[0] < 15.0:
        return _SVC_CACHE[1]
    running = set()
    for line in sh("systemctl list-units --type=service --state=running "
                   "--no-legend --plain").splitlines():
        p = line.split()
        if p:
            running.add(p[0].replace(".service", ""))
    if not (_UF_CACHE[0] and now - _UF_CACHE[0] < 300.0):
        installed = {}
        for line in sh("systemctl list-unit-files --type=service "
                       "--no-legend --plain").splitlines():
            p = line.split()
            if p:
                installed[p[0].replace(".service", "")] = p[1] if len(p) > 1 else ""
        _UF_CACHE[0], _UF_CACHE[1] = now, installed
    installed = _UF_CACHE[1]
    names = []
    for s in WATCH_SERVICES:
        if s in installed or s in running:
            names.append(s)
    for s in sorted(installed):          # 补齐本机特有的 roceos-* 服务
        if not s.startswith("roceos") or s in names or "@" in s:
            continue                     # 跳过 xxx@ 这类模板单元
        if s in running or installed.get(s) in (
                "enabled", "enabled-runtime", "indirect", "generated", "alias", "static"):
            names.append(s)
    if len(names) < 4:
        extra = [s for s in sorted(running)
                 if not any(s.startswith(x) for x in SYS_NOISE) and s not in names]
        names += extra[:16 - len(names)]
    out = [{"name": s, "active": s in running} for s in names]
    _SVC_CACHE[0], _SVC_CACHE[1] = now, out
    return out

_PPP_NIC_CACHE = [0.0, []]

def pppoe_nics():
    """PPPoE 的物理承载口（如 eth1）。

    ppp0 只是逻辑口，真正跑流量的是它底下的物理口；不标出来的话 eth1 会被当成
    LAN 口，看起来就像局域网在上行几十 KB/s。pppd 命令行里没有网卡名，写在它
    call 的 peers 文件里（rp-pppoe.so 的 nic-ethX 参数）。结果缓存 5 分钟。
    """
    now = time.time()
    if now - _PPP_NIC_CACHE[0] < 300.0:
        return _PPP_NIC_CACHE[1]
    out = []
    try:
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/cmdline" % pid, "rb") as f:
                    cmd = f.read().replace(b"\0", b" ").decode("utf-8", "ignore")
            except Exception:
                continue
            if "pppd" not in cmd:
                continue
            m = re.search(r"\bcall\s+(\S+)", cmd)
            if not m:
                continue
            txt = read("/etc/ppp/peers/%s" % m.group(1))
            got = re.findall(r"nic-(\S+)", txt)
            if not got:
                got = re.findall(r"\b(eth\d+(?:\.\d+)?|enp\w+|eno\d+|ens\d+|enx\w+)\b", txt)
            for n in got:
                n = n.strip()
                if n and os.path.isdir("/sys/class/net/" + n) and n not in out:
                    out.append(n)
    except Exception:
        pass
    _PPP_NIC_CACHE[0], _PPP_NIC_CACHE[1] = now, out
    return out


def collect_all():
    sysinfo = collect_system()
    ifaces = collect_ifaces()
    wan = collect_wan(ifaces)
    # 网络总览只统计"经过 WAN 口"的流量：局域网内部互访不经过它，自然不计入
    wi = ifaces.get(wan.get("iface"), {}) or {}
    wan_rate = {
        "iface": wan.get("iface", "--"),
        "rx": wi.get("rx_rate", 0.0), "tx": wi.get("tx_rate", 0.0),
        "rx_text": wi.get("rx_rate_text", "0B/s"),
        "tx_text": wi.get("tx_rate_text", "0B/s"),
        "rx_bytes": wi.get("rx_bytes", 0), "tx_bytes": wi.get("tx_bytes", 0),
        "rx_total_text": wi.get("rx_text", "--"),
        "tx_total_text": wi.get("tx_text", "--"),
    }
    return {
        "ts": time.time(),
        "time": datetime.now().strftime("%H:%M:%S"),
        "port": ACTUAL_PORT,
        "system": sysinfo,
        "core_usage": SAMPLER.core_usage(),
        "ifaces": ifaces,
        "wan": wan,
        "wan_rate": wan_rate,
        "wan_nics": pppoe_nics(),
        "services": collect_services(),
        "sources": {"db": db_path() or "", "leases": lease_file() or "",
                    "conntrack": SAMPLER.ct_ok, "ct_live": SAMPLER.ct_live,
                    "sniff": SNIFFER.active,
                    "sniff_ifaces": SNIFFER.ifaces},
    }

# ---------- 前端页面 ----------
HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>路由看板 · iNextOS</title>
<style>
:root{
  --bg:#f4f6fa; --card:#ffffff; --card2:#f6f8fc;
  --line:#e3e9f2; --line2:#edf1f7;
  --txt:#1b2434; --dim:#64748b; --dim2:#94a3b8;
  --accent:#16a34a; --accent2:#0284c7; --warn:#f59e0b; --bad:#dc2626; --purple:#7c3aed;
  --ring:#e8edf5;
  --shadow:0 1px 2px rgba(16,24,40,.05), 0 6px 18px rgba(16,24,40,.06);
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--txt);font:14px/1.5 "Segoe UI","PingFang SC","Microsoft YaHei",system-ui,sans-serif;
     padding:18px;min-height:100vh;-webkit-text-size-adjust:100%}
a{color:var(--accent2)}
h1{font-size:18px;font-weight:600}
h2{font-size:12.5px;font-weight:700;color:var(--dim);letter-spacing:.6px;margin-bottom:12px;text-transform:uppercase}
.topbar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:14px;padding:14px 18px;
  background:linear-gradient(135deg,#ffffff,#f7f9fd);border:1px solid var(--line);border-radius:16px;
  box-shadow:var(--shadow);margin-bottom:16px}
.brand{display:flex;align-items:center;gap:12px;min-width:0}
.logo{width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,var(--accent2),var(--accent));
  display:flex;align-items:center;justify-content:center;font-weight:700;font-size:19px;color:#fff;flex:0 0 auto}
.sub{color:var(--dim);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.topstats{display:flex;gap:22px;flex-wrap:wrap}
.ts{text-align:right}
.ts label{display:block;font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px}
.ts b{font-size:16px;font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--accent);margin-left:6px}
.dot.off{background:#cbd5e1}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px;margin-bottom:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;box-shadow:var(--shadow)}
.card.wide{grid-column:span 2}
.card h2{margin-bottom:14px}
section{margin-bottom:16px}
.sec-title{font-size:12.5px;color:var(--dim);text-transform:uppercase;letter-spacing:.6px;margin:0 0 10px 2px;font-weight:700}
.ringwrap{position:relative;width:132px;height:132px;margin:0 auto 10px}
.ring{transform:rotate(-90deg)}
.ring-bg{fill:none;stroke:var(--ring);stroke-width:11}
.ring-fg{fill:none;stroke:var(--accent);stroke-width:11;stroke-linecap:round;transition:stroke-dasharray .5s,stroke .5s}
.ringtext{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center}
.ringtext b{font-size:26px;font-variant-numeric:tabular-nums}
.ringtext span{font-size:10px;color:var(--dim);max-width:110px;text-align:center;line-height:1.2}
/* ---- 仪表盘（CPU / 内存）---- */
.gauges{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.gauge{background:var(--card2);border:1px solid var(--line2);border-radius:14px;padding:10px 10px 12px;text-align:center}
.gauge .gtop{font-size:11px;color:var(--dim);letter-spacing:.5px;text-transform:uppercase;font-weight:700;margin-bottom:2px}
.gwrap{position:relative;width:100%;max-width:190px;margin:0 auto}
.gwrap svg{width:100%;display:block}
.gbg{fill:none;stroke:var(--ring);stroke-width:11;stroke-linecap:round}
.gfg{fill:none;stroke:var(--accent);stroke-width:11;stroke-linecap:round;
     transition:stroke-dasharray .5s ease,stroke .5s ease}
.gval{position:absolute;left:0;right:0;bottom:6px;text-align:center;pointer-events:none}
.gval b{font-size:26px;font-variant-numeric:tabular-nums;line-height:1}
.gcap{font-size:11px;color:var(--dim);margin-top:2px;line-height:1.35}
.gtick{display:flex;justify-content:space-between;font-size:9.5px;color:var(--dim2);padding:0 4px;margin-top:-4px}
.gmeter{height:6px;background:var(--ring);border-radius:4px;overflow:hidden;margin:8px 2px 0}
.gmeter>i{display:block;height:100%;border-radius:4px;background:linear-gradient(90deg,var(--accent2),var(--accent));transition:width .5s}
.gmeter.warn>i{background:linear-gradient(90deg,var(--warn),#f97316)}
.gmeter.bad>i{background:linear-gradient(90deg,var(--bad),#ef4444)}
.gsub{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);margin-top:7px;gap:8px}
.gsub b{color:var(--txt);font-variant-numeric:tabular-nums;font-weight:600}
.cores{display:grid;grid-template-columns:repeat(2,1fr);gap:6px;margin-top:10px}
.core{background:var(--card2);border:1px solid var(--line2);border-radius:8px;padding:5px 8px;font-size:11px;
  color:var(--dim);display:flex;justify-content:space-between}
.core b{color:var(--txt);font-variant-numeric:tabular-nums}
.bar{height:9px;background:var(--ring);border-radius:5px;overflow:hidden;margin:6px 0 4px}
.bar>i{display:block;height:100%;border-radius:5px;background:linear-gradient(90deg,var(--accent2),var(--accent));transition:width .5s}
.bar.warn>i{background:linear-gradient(90deg,var(--warn),#f97316)}
.bar.bad>i{background:linear-gradient(90deg,var(--bad),#ef4444)}
.kv{display:flex;justify-content:space-between;padding:6px 0;font-size:13px;border-bottom:1px dashed var(--line2);gap:10px}
.kv:last-child{border:0}
.kv span{color:var(--dim);flex:0 0 auto}
.kv b{font-variant-numeric:tabular-nums;font-weight:600;text-align:right;word-break:break-all}
.big{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--txt)}
.big.sm{font-size:19px}
.muted{color:var(--dim);font-size:12px}
.wanip{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;color:#0f766e;background:#f0fdfa;
  border:1px solid #ccece6;border-radius:10px;padding:8px 12px;margin:2px 0 3px;letter-spacing:.3px;word-break:break-all}
.dnslist{margin-top:5px}
.dnslist div{background:var(--card2);border:1px solid var(--line);border-radius:8px;padding:6px 10px;margin-bottom:5px;
  font-family:ui-monospace,Consolas,monospace;font-size:12.5px;display:flex;justify-content:space-between;
  align-items:center;gap:10px}
.dnslist div span{color:var(--dim);font-size:10.5px;font-family:inherit;flex:0 0 auto}
canvas{width:100%;height:70px;display:block;margin-top:8px}
.ifgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:14px}
.ifcard{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:15px;box-shadow:var(--shadow)}
.ifcard.up{border-color:#b7e4c7}
.ifcard.wan{border-color:#bae6fd;box-shadow:0 1px 2px rgba(2,132,199,.08),0 6px 18px rgba(2,132,199,.10)}
.ifcard.wan{border-color:#bae6fd;background:linear-gradient(180deg,#f7fdff,#ffffff)}
.tag.w{background:#e0f2fe;color:#0369a1;border-color:#bae6fd}
.ifhead{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:8px}
.ifname{display:flex;align-items:center;gap:8px;font-size:15px;font-weight:600}
.led{width:9px;height:9px;border-radius:50%;background:#cbd5e1;box-shadow:0 0 0 3px rgba(203,213,225,.35)}
.led.up{background:var(--accent);box-shadow:0 0 0 3px rgba(22,163,74,.18)}
.led.down{background:var(--bad);box-shadow:0 0 0 3px rgba(220,38,38,.14)}
.tag{font-size:10px;padding:2px 8px;border-radius:20px;background:var(--card2);color:var(--dim);
  border:1px solid var(--line);white-space:nowrap}
.tag.phys{background:#e8f6ee;color:#15803d;border-color:#b7e4c7}
.tag.virt{background:#f1ecfd;color:#6d28d9;border-color:#ddd0fb}
.tag.wan{background:#e0f2fe;color:#0369a1;border-color:#bae6fd}
.rates{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:10px 0 4px}
.rate{background:var(--card2);border:1px solid var(--line2);border-radius:10px;padding:8px 10px}
.rate label{font-size:10px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;display:flex;align-items:center;gap:5px}
.rate b{font-size:16px;font-variant-numeric:tabular-nums}
.rx{color:var(--accent2)} .tx{color:var(--purple)}
.rate b.rx,.rate b.tx{color:var(--txt)}
.ifi{display:flex;justify-content:space-between;font-size:11px;color:var(--dim);margin-top:3px;gap:8px}
.two{display:grid;grid-template-columns:1.6fr 1fr;gap:14px;align-items:start}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.5px;padding:9px 8px;
  border-bottom:1px solid var(--line);font-weight:700;background:var(--card2);white-space:nowrap}
td{padding:9px 8px;border-bottom:1px solid var(--line2);font-variant-numeric:tabular-nums}
tr:last-child td{border:0}
td.ip{font-weight:600}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px;color:var(--dim)}
.pill{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:600}
.pill.on{background:#e8f6ee;color:#15803d}
.pill.off{background:#f1f5f9;color:var(--dim)}
.svcs{display:flex;flex-wrap:wrap;gap:7px}
.svc{font-size:12px;padding:5px 11px;border-radius:20px;background:var(--card2);border:1px solid var(--line);
  color:var(--dim);display:flex;align-items:center;gap:6px}
.svc i{width:6px;height:6px;border-radius:50%;background:#cbd5e1;display:inline-block}
.svc.on i{background:var(--accent)}
.svc.on{color:var(--txt);border-color:#b7e4c7;background:#f4fbf7}
.empty{color:var(--dim);padding:20px;text-align:center;font-size:13px}
.foot{text-align:center;color:var(--dim);font-size:11px;padding:14px 0;line-height:1.7}
.drate{display:flex;flex-direction:column;gap:1px;min-width:106px}
.drate i{font-style:normal;font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.drate .u{color:#0369a1} .drate .d{color:#7c3aed}
.sparkbar{height:4px;border-radius:3px;background:var(--ring);overflow:hidden;margin:3px 0 2px;width:110px}
.sparkbar>i{display:block;height:100%;background:linear-gradient(90deg,var(--accent2),#7c3aed)}
/* ---------------- 平板竖屏 ---------------- */
@media(max-width:1180px){
  .two{grid-template-columns:1fr}
  .grid{grid-template-columns:repeat(2,minmax(0,1fr))}
  .card.wide{grid-column:span 2}
}
/* ---------------- 手机 / 窄屏 ---------------- */
@media(max-width:720px){
  .card.wide{grid-column:span 1}
  .gauges{grid-template-columns:1fr;gap:10px}
  body{padding:10px}
  .topbar{padding:12px 14px;gap:10px}
  .topstats{width:100%;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 12px}
  .ts{text-align:left}
  .grid{grid-template-columns:1fr;gap:12px}
  .ifgrid{grid-template-columns:1fr}
  .card{padding:14px;border-radius:14px}
  .ringwrap{width:118px;height:118px}
  .sub{white-space:normal}
  table,thead,tbody,tr,td{display:block;width:100%}
  thead{display:none}
  tr{background:var(--card2);border:1px solid var(--line);border-radius:12px;margin:0 0 10px;padding:10px 12px}
  td{border:0;padding:5px 0;display:flex;justify-content:space-between;align-items:flex-start;gap:14px}
  td::before{content:attr(data-label);color:var(--dim);font-size:12px;flex:0 0 auto;font-weight:600;padding-top:1px}
  td>div,td>span{text-align:right;min-width:0}
  .drate{min-width:0;align-items:flex-end}
  .sparkbar{width:100%;max-width:160px}
  .svc{padding:6px 12px}
}
/* ---------------- 手机横屏（矮屏） ---------------- */
@media(max-height:520px) and (orientation:landscape){
  body{padding:8px}
  .topbar{margin-bottom:10px;padding:10px 14px}
  .ringwrap{width:100px;height:100px}
  canvas{height:54px}
  .cores{grid-template-columns:repeat(4,1fr)}
}
/* ---------------- 大屏 / 显示器 ---------------- */
@media(min-width:1500px){
  body{max-width:1680px;margin:0 auto}
  .grid{grid-template-columns:repeat(4,minmax(0,1fr))}
  .card.wide{grid-column:span 2}
  .card.wide{grid-column:span 2}
  .ifgrid{grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">
    <div class="logo">R</div>
    <div>
      <h1 id="hostname">--</h1>
      <div class="sub"><span id="cpuModel">--</span> · <span id="kernel">--</span> · 看板端口 <b id="port">--</b></div>
    </div>
  </div>
  <div class="topstats">
    <div class="ts"><label>运行时间</label><b id="uptime">--</b></div>
    <div class="ts"><label>1分钟负载</label><b id="load1">--</b></div>
    <div class="ts"><label>连接数</label><b id="conn">--</b></div>
    <div class="ts"><label>最后更新</label><b id="lastupd">--</b><span class="dot" id="pulse"></span></div>
  </div>
</header>

<div class="grid">
  <div class="card wide">
    <h2>CPU / 内存占用</h2>
    <div class="gauges">
      <div class="gauge">
        <div class="gtop">CPU</div>
        <div class="gwrap">
          <svg viewBox="0 0 120 70" preserveAspectRatio="xMidYMid meet">
            <path class="gbg" d="M12 62 A48 48 0 0 1 108 62"/>
            <path class="gfg" id="cpuArc" d="M12 62 A48 48 0 0 1 108 62" stroke-dasharray="0 999"/>
          </svg>
          <div class="gval"><b id="cpuPct">0%</b></div>
        </div>
        <div class="gcap" id="cores_n">-- 核</div>
        <div class="gtick"><span>0</span><span>50</span><span>100</span></div>
        <div class="gmeter" id="cpuMeterWrap"><i id="cpuMeter" style="width:0"></i></div>
        <div class="gsub"><span>温度 <b id="temp">--</b></span><span>进程 <b id="procs">--</b></span></div>
      </div>
      <div class="gauge">
        <div class="gtop">内存</div>
        <div class="gwrap">
          <svg viewBox="0 0 120 70" preserveAspectRatio="xMidYMid meet">
            <path class="gbg" d="M12 62 A48 48 0 0 1 108 62"/>
            <path class="gfg" id="memArc" d="M12 62 A48 48 0 0 1 108 62" stroke-dasharray="0 999"/>
          </svg>
          <div class="gval"><b id="memPct">0%</b></div>
        </div>
        <div class="gcap" id="memText">--</div>
        <div class="gtick"><span>0</span><span>50</span><span>100</span></div>
        <div class="gmeter" id="memMeterWrap"><i id="memMeter" style="width:0"></i></div>
        <div class="gsub"><span>缓存 <b id="memCache">--</b></span><span>SWAP <b id="swapText">--</b></span></div>
      </div>
    </div>
    <div class="cores" id="cores"></div>
    <div class="kv" style="margin-top:10px"><span>内存已用 / 总计</span><b id="memUse">--</b></div>
  </div>

  <div class="card">
    <h2>系统负载</h2>
    <div class="big" id="loadAvg">--</div>
    <div class="muted">1 / 5 / 15 分钟</div>
    <canvas id="loadSpark" style="height:88px"></canvas>
    <div class="kv"><span>CPU 历史</span><b id="cpuAvg">--</b></div>
    <div class="kv"><span>网口（物理 / 在用）</span><b id="ifcount">--</b></div>
  </div>

  <div class="card">
    <h2>网络总览 <span class="tag" id="wanTag">WAN</span></h2>
    <div class="big sm" id="totalRate">--</div>
    <div class="muted" style="margin-bottom:10px">实时下行 / 上行<span id="wanRateIf"></span>（仅统计经过 WAN 口的流量，局域网内互访不计入）</div>
    <div class="kv"><span>WAN 累计 ↓ / ↑</span><b id="wanTotal">--</b></div>
    <div class="kv"><span>连接跟踪</span><b id="conn2">--</b></div>
    <div class="kv"><span>内核</span><b id="kernel2">--</b></div>
    <canvas id="wanSpark" style="height:60px"></canvas>
  </div>
</div>

<section>
  <div class="sec-title">网口实时流量（WAN 与 LAN 口，虚拟网卡工作时才出现）</div>
  <div class="ifgrid" id="ifgrid"></div>
</section>

<section class="two">
  <div class="card">
    <h2>广域网 WAN <span class="tag" id="wanIf">--</span></h2>
      <div class="kv"><span>接入类型</span><b id="wanType">--</b></div>
      <div class="kv"><span>连接状态</span><b id="wanState">--</b></div>
      <div class="muted" style="margin-top:8px">WAN 口 IP 地址</div>
      <div class="wanip" id="wanIp">--</div>
      <div class="muted" style="margin-top:8px">DNS 服务器<span id="dnsSrc"></span></div>
      <div class="dnslist" id="wanDns"></div>
      <div class="kv"><span>网关 / 对端</span><b id="wanPeer">--</b></div>
      <div class="kv"><span>MTU</span><b id="wanMtu">--</b></div>
      <div class="kv"><span>已连接</span><b id="wanUp">--</b></div>
      <div class="kv"><span>累计 ↓ / ↑</span><b id="wanTraffic">--</b></div>
      <div class="kv"><span>实时 ↓ / ↑</span><b id="wanRate">--</b></div>
  </div>
  <div>
    <div class="card">
      <h2>服务状态</h2>
      <div class="svcs" id="svcs"></div>
    </div>
  </div>
</section>

<div class="foot">每 3 秒自动刷新 · 数据来源：<span id="srcInfo">/proc · /sys/class/net · 路由表 · resolv.conf</span></div>

<script>
const H = {cpu:[],load:[],mem:[],rx:{},tx:{},wanrx:[],wantx:[]};
const MAXP = 60;
const $ = id => document.getElementById(id);
const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function drawSpark(cv, data, color, max){
  const dpr = window.devicePixelRatio||1;
  const w = cv.clientWidth, h = cv.clientHeight;
  if(cv.width!==w*dpr||cv.height!==h*dpr){cv.width=w*dpr;cv.height=h*dpr;}
  const g = cv.getContext('2d'); g.setTransform(dpr,0,0,dpr,0,0); g.clearRect(0,0,w,h);
  if(data.length<2) return;
  const mx = max || Math.max.apply(null,data)*1.25 || 1;
  const X = i => i/(data.length-1)*(w-2)+1;
  const Y = v => h-2 - (v/mx)*(h-6);
  g.beginPath(); g.moveTo(X(0),h);
  data.forEach((v,i)=>g.lineTo(X(i),Y(v)));
  g.lineTo(X(data.length-1),h); g.closePath();
  const grd = g.createLinearGradient(0,0,0,h);
  grd.addColorStop(0,color+'55'); grd.addColorStop(1,color+'00');
  g.fillStyle=grd; g.fill();
  g.beginPath(); data.forEach((v,i)=> i?g.lineTo(X(i),Y(v)):g.moveTo(X(i),Y(v)));
  g.strokeStyle=color; g.lineWidth=1.6; g.lineJoin='round'; g.stroke();
}
function push(arr,v){arr.push(v); if(arr.length>MAXP) arr.shift(); return arr;}
function setBar(el,wrap,pct){
  el.style.width = Math.min(100,pct)+'%';
  wrap.className = 'bar'+(pct>85?' bad':pct>65?' warn':'');
}
/* 半圆仪表盘：半径 48 的半圆弧长 = π*48 */
const GARC = Math.PI*48;
const gcolor = v => v>85?'#dc2626':v>65?'#f59e0b':'#16a34a';
function setGauge(id,pct){
  const p = Math.max(0,Math.min(100,Number(pct)||0));
  const el = $(id);
  el.setAttribute('stroke-dasharray',(GARC*p/100).toFixed(2)+' '+GARC.toFixed(2));
  el.style.stroke = gcolor(p);
}
function setMeter(id,wrapId,pct){
  const p = Math.max(0,Math.min(100,Number(pct)||0));
  $(id).style.width = p+'%';
  $(wrapId).className = 'gmeter'+(p>85?' bad':p>65?' warn':'');
}
function fmtB(n){n=Number(n)||0;const u=['B','K','M','G','T'];let i=0;while(n>=1024&&i<4){n/=1024;i++}return (i?n.toFixed(1):n.toFixed(0))+u[i];}
function fmtR(n){return fmtB(n)+'/s';}

let cardsBuilt = {};

function render(d){
  const s = d.system;
  $('hostname').textContent = s.hostname;
  $('cpuModel').textContent = (s.cpu_model||'').replace(/\s+/g,' ').slice(0,42);
  $('kernel').textContent = s.kernel;
  $('uptime').textContent = s.uptime_text;
  $('load1').textContent = s.load[0].toFixed(2);
  $('conn').textContent = s.conntrack!=null ? s.conntrack : '--';
  $('lastupd').textContent = d.time;
  $('port').textContent = d.port || location.port;
  const p = $('pulse'); p.classList.toggle('off', p.classList.contains('off'));

  // CPU 仪表盘
  push(H.cpu, s.cpu); push(H.load, s.load[0]); push(H.mem, s.mem.percent);
  $('cpuPct').textContent = s.cpu.toFixed(1)+'%';
  $('cores_n').textContent = s.cores+' 核 · '+s.procs+' 进程';
  setGauge('cpuArc', s.cpu);
  setMeter('cpuMeter','cpuMeterWrap', s.cpu);
  $('procs').textContent = s.procs;
  $('temp').textContent = (s.temp!=null? s.temp.toFixed(0)+' °C':'--');
  const ca = H.cpu.length? (H.cpu.reduce((a,b)=>a+b,0)/H.cpu.length) : 0;
  $('cpuAvg').textContent = ca.toFixed(1)+'% 均值（近 '+H.cpu.length+' 次）';

  // 每核
  const cw = $('cores');
  if(cw.children.length !== s.cores){
    cw.innerHTML=''; for(let i=0;i<s.cores;i++){const e=document.createElement('div');e.className='core';e.innerHTML='<span>CPU'+i+'</span><b>--</b>';cw.appendChild(e);}
  }
  if(d.core_usage && d.core_usage.length===s.cores){
    Array.from(cw.children).forEach((e,i)=>{e.querySelector('b').textContent=d.core_usage[i].toFixed(0)+'%';});
  }

  // 内存仪表盘
  const m = s.mem;
  $('memPct').textContent = m.percent.toFixed(0)+'%';
  $('memText').textContent = fmtB(m.used)+' / '+fmtB(m.total);
  $('memUse').textContent = fmtB(m.used)+' / '+fmtB(m.total);
  $('memCache').textContent = fmtB(m.cached);
  $('swapText').textContent = fmtB(m.swap_used)+' / '+fmtB(m.swap_total);
  setGauge('memArc', m.percent);
  setMeter('memMeter','memMeterWrap', m.percent);

  // 负载
  $('loadAvg').textContent = s.load.map(x=>x.toFixed(2)).join(' / ');
  drawSpark($('loadSpark'),H.load,'#7c3aed');

  $('conn2').textContent = (s.conntrack!=null && s.conntrack_max)
      ? s.conntrack.toLocaleString()+' / '+s.conntrack_max.toLocaleString() : '--';
  $('kernel2').textContent = s.kernel;
  $('srcInfo').textContent = '/proc · /sys/class/net · 路由表 · resolv.conf'
      + ' · 速率由网口计数器差值计算（3 秒窗口）';

  // 网口（WAN / LAN 各自实时上下行）
  const ifs = Object.values(d.ifaces);
  const wanIf = (d.wan && d.wan.iface) || (d.wan_rate && d.wan_rate.iface) || '';
  const wanNics = d.wan_nics || [];
  $('ifcount').textContent = ifs.filter(x=>x.physical).length+' / '+ifs.length;
  const grid = $('ifgrid');
  // 移除已经消失的网卡卡片（虚拟网卡停止工作时）
  const alive = new Set(ifs.map(x=>x.name));
  Object.keys(cardsBuilt).forEach(n=>{
    if(!alive.has(n)){ cardsBuilt[n].el.remove(); delete cardsBuilt[n]; delete H.rx[n]; delete H.tx[n]; }
  });
  ifs.forEach(it=>{
    let c = cardsBuilt[it.name];
    if(!c){
      const el = document.createElement('div'); el.className='ifcard';
      el.innerHTML =
        '<div class="ifhead"><div class="ifname"><span class="led" data-led></span><span>'+esc(it.name)+'</span>'
        +'<span class="tag" data-tag></span></div><span class="muted" data-ip></span></div>'
        +'<div class="rates">'
        +'<div class="rate"><label><i class="rx">▼</i>下行</label><b class="rx" data-rx>--</b></div>'
        +'<div class="rate"><label><i class="tx">▲</i>上行</label><b class="tx" data-tx>--</b></div></div>'
        +'<canvas data-cv style="height:52px"></canvas>'
        +'<div class="ifi"><span data-mac class="mono"></span><span data-state></span></div>'
        +'<div class="ifi"><span>累计 ↓<b data-rxt>0</b></span><span>↑<b data-txt>0</b></span></div>'
        +'<div class="ifi"><span data-speed></span><span data-pkt></span></div>';
      grid.appendChild(el);
      c = cardsBuilt[it.name] = {
        el, led:el.querySelector('[data-led]'), tag:el.querySelector('[data-tag]'),
        ip:el.querySelector('[data-ip]'), rx:el.querySelector('[data-rx]'),
        tx:el.querySelector('[data-tx]'), cv:el.querySelector('[data-cv]'),
        mac:el.querySelector('[data-mac]'), state:el.querySelector('[data-state]'),
        rxt:el.querySelector('[data-rxt]'), txt:el.querySelector('[data-txt]'),
        speed:el.querySelector('[data-speed]'), pkt:el.querySelector('[data-pkt]')
      };
      H.rx[it.name]=[]; H.tx[it.name]=[];
    }
    push(H.rx[it.name], it.rx_rate); push(H.tx[it.name], it.tx_rate);
    const up = it.carrier || (it.state === 'up') || (it.state === 'unknown' && (it.ip.length || it.ipv6.length));
    c.el.classList.toggle('up', up);
    c.led.className = 'led ' + (up?'up':'down');
    const isWan = (wanIf && it.name === wanIf) || wanNics.indexOf(it.name) >= 0;
    c.el.classList.toggle('wan', isWan);
    c.tag.className = 'tag ' + (isWan ? 'w' : (it.physical ? 'phys' : 'virt'));
    c.tag.textContent = isWan ? ('WAN ' + (it.name === wanIf ? '口' : '承载口'))
        : (it.physical ? ('LAN 口' + (it.vendor ? ' · ' + it.vendor : '')) : '虚拟接口');
    c.ip.textContent = it.ip.length? it.ip.join(', ') : (it.ipv6.length? 'IPv6 only':'未分配');
    c.rx.textContent = fmtR(it.rx_rate);
    c.tx.textContent = fmtR(it.tx_rate);
    c.rxt.textContent = fmtB(it.rx_bytes);
    c.txt.textContent = fmtB(it.tx_bytes);
    c.mac.textContent = it.mac + (it.permaddr && it.permaddr!==it.mac ? ' (硬件 '+it.permaddr+')':'');
    c.state.textContent = it.state + (it.speed>0? ' · '+it.speed+'Mbps':'') + ' · MTU '+it.mtu;
    c.speed.textContent = '↓'+it.rx_packets.toLocaleString()+' / ↑'+it.tx_packets.toLocaleString()+' 包';
    c.pkt.textContent = (it.rx_errors+it.tx_errors)? '错误 '+(it.rx_errors+it.tx_errors):'无丢包';
    drawSpark(c.cv, H.rx[it.name], '#0284c7');
  });

  // 网络总览：只算经过 WAN 口的流量（局域网互访不经过 WAN，不计入）
  const wr = d.wan_rate || {};
  $('totalRate').textContent = fmtR(wr.rx) + ' / ' + fmtR(wr.tx);
  $('wanTag').textContent = wr.iface || 'WAN';
  $('wanRateIf').textContent = wr.iface ? ' · ' + wr.iface + ' 口' : '';
  $('wanTotal').textContent = (wr.rx_total_text||'--') + ' / ' + (wr.tx_total_text||'--');
  push(H.wanrx, wr.rx||0); push(H.wantx, wr.tx||0);
  drawSpark($('wanSpark'), H.wanrx, '#0284c7');

  // WAN
  const w = d.wan || {};
  $('wanIf').textContent = w.iface || '--';
  $('wanType').textContent = w.type || '--';
  $('wanState').innerHTML = w.connected
      ? '<span class="pill on">已连接</span>' : '<span class="pill off">未连接</span>';
  $('wanIp').textContent = w.ip || '--';
  $('wanPeer').textContent = w.peer || '直连 / 无';
  $('wanUp').textContent = w.uptime_text || '--';
  $('wanMtu').textContent = w.mtu || '--';
  $('wanTraffic').textContent = (w.rx_text||'--') + ' / ' + (w.tx_text||'--');
  $('wanRate').textContent = (w.rx_rate_text||'--') + ' / ' + (w.tx_rate_text||'--');
  const dl = $('wanDns');
  if(w.dns && w.dns.length){
    dl.innerHTML = w.dns.map((x,i)=>'<div>'+esc(x)
      +'<span>'+(i===0?'主 DNS':(i===1?'备 DNS':'DNS '+(i+1)))+'</span></div>').join('');
  }else{
    dl.innerHTML = '<div style="color:var(--dim)">未获取到 DNS</div>';
  }
  $('dnsSrc').textContent = w.dns_src ? '（来源 ' + esc(w.dns_src) + '）' : '';

  const sl = $('svcs');
  sl.innerHTML = (d.services && d.services.length)
    ? d.services.map(s2=>'<span class="svc '+(s2.active?'on':'')+'"><i></i>'+esc(s2.name)+'</span>').join('')
    : '<span class="muted">未检测到服务信息</span>';
  drawCpuHist();
}
function drawCpuHist(){ /* 顶部 CPU 历史并入负载图 */ }

let fail = 0;
async function tick(){
  try{
    const r = await fetch('/api/stats?t='+Date.now(), {cache:'no-store'});
    const d = await r.json();
    render(d);
    $('pulse').classList.remove('off');
    fail = 0;
  }catch(e){
    fail++;
    $('pulse').classList.add('off');
    if(fail===3) $('hostname').textContent = '连接中断，正在重试…';
  }
}
tick();
setInterval(tick, 3000);
</script>
</body>
</html>
"""

# ---------- HTTP ----------
INSTALL_SH = None

def build_installer():
    src = read(os.path.abspath(__file__)) if os.path.exists(os.path.abspath(__file__)) else ""
    if not src:
        src = "# 无法读取自身源码\n"
    # 注意：源码里含有 heredoc 定界符本身，必须 base64 编码后写入，否则会被提前截断
    import base64
    b64 = base64.b64encode(src.encode("utf-8")).decode("ascii")
    b64_lines = "\n".join(b64[i:i + 100] for i in range(0, len(b64), 100))
    tmpl = """#!/bin/bash
# 路由看板 —— 一键部署脚本（iNextOS / Debian / Ubuntu 通用，仅需 python3 标准库）
# 用法 1（本机已有脚本）： bash install.sh
# 用法 2（换端口重装）  ： ROCEOS_DASH_PORT=8080 bash install.sh
# 用法 3（从另一台机器拉）： curl -s http://<路由IP>:__PORT__/install.sh | bash
set -e
PORT=${ROCEOS_DASH_PORT:-__PORT__}

# --- 0. 找到 python3（不同发行版路径不同，别写死）---
PY=$(command -v python3 || command -v python || true)
if [ -z "$PY" ]; then
  echo "错误：未找到 python3。请先安装： apt-get update && apt-get install -y python3"
  exit 1
fi
echo "[1/5] 使用解释器: $PY  ($($PY -V 2>&1))"

if ! command -v base64 >/dev/null 2>&1; then
  echo "错误：缺少 base64 命令，请先安装 coreutils"
  exit 1
fi

# --- 1. 写入源码（base64 方式，避免源码里的定界符截断 heredoc）---
mkdir -p /opt/roceos-dash
cat > /opt/roceos-dash/dash.py.b64 <<'ROCEOS_DASH_B64'
__B64__
ROCEOS_DASH_B64
base64 -d /opt/roceos-dash/dash.py.b64 > /opt/roceos-dash/dash.py
rm -f /opt/roceos-dash/dash.py.b64
chmod +x /opt/roceos-dash/dash.py
echo "[2/5] 已安装到 /opt/roceos-dash/dash.py"

# --- 2. 语法自检，坏了就别往下走 ---
if ! "$PY" -m py_compile /opt/roceos-dash/dash.py >/dev/null 2>&1; then
  echo "错误：源码语法检查失败，请检查 /opt/roceos-dash/dash.py"
  exit 1
fi

# --- 3. 保存一份安装脚本本体，方便以后重装/换端口 ---
# 注意：curl | bash 时 $0 是 bash 本身，必须校验文件名，不能盲目复制
case "$0" in
  *install.sh)
    SPATH=$(readlink -f "$0" 2>/dev/null || echo "$0")
    if [ "$SPATH" != "/opt/roceos-dash/install.sh" ]; then
      cp "$SPATH" /opt/roceos-dash/install.sh 2>/dev/null && chmod +x /opt/roceos-dash/install.sh || true
    fi
    ;;
esac

# --- 4. systemd 单元 ---
cat > /etc/systemd/system/roceos-dash.service <<ROCEOS_UNIT_EOF
[Unit]
Description=iNextOS Router Dashboard
After=network.target

[Service]
Type=simple
Environment=ROCEOS_DASH_PORT=$PORT
ExecStart=$PY /opt/roceos-dash/dash.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
ROCEOS_UNIT_EOF
if command -v systemctl >/dev/null 2>&1; then
  systemctl daemon-reload || true
  systemctl enable roceos-dash.service >/dev/null 2>&1 || true
  systemctl restart roceos-dash.service \
    || { echo "systemd 启动失败，改为后台直接运行"; nohup "$PY" /opt/roceos-dash/dash.py >/tmp/roceos-dash.log 2>&1 & }
  echo "[3/5] 服务 roceos-dash 已启动并设为开机自启（端口 $PORT）"
else
  echo "未检测到 systemd，改为后台直接运行"
  nohup "$PY" /opt/roceos-dash/dash.py >/tmp/roceos-dash.log 2>&1 &
fi

# --- 5. 防火墙放行（幂等；没有 nft/iptables 或权限不足就跳过，不影响安装）---
if command -v nft >/dev/null 2>&1 && nft list ruleset >/dev/null 2>&1; then
  if ! nft list ruleset 2>/dev/null | grep -q "dport $PORT"; then
    nft list tables 2>/dev/null | while read -r _ FAM TBL; do
      if nft list chain "$FAM" "$TBL" input >/dev/null 2>&1; then
        nft insert rule "$FAM" "$TBL" input tcp dport "$PORT" accept >/dev/null 2>&1 \
          && echo "[4/5] 已放行防火墙：$FAM/$TBL input 允许 tcp/$PORT" || true
      fi
    done
  else
    echo "[4/5] 防火墙已放行过 $PORT，跳过"
  fi
elif command -v iptables >/dev/null 2>&1; then
  iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT >/dev/null 2>&1 \
    || iptables -I INPUT -p tcp --dport "$PORT" -j ACCEPT >/dev/null 2>&1 \
    && echo "[4/5] 已放行 iptables tcp/$PORT" || echo "[4/5] 防火墙未改动（无权限或无规则表）"
else
  echo "[4/5] 未检测到 nft/iptables，跳过防火墙配置"
fi

# --- 6. 健康检查 ---
LANIP=$(ip -o -4 addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
if [ -z "$LANIP" ]; then LANIP=$(hostname -I 2>/dev/null | awk '{print $1}'); fi
if [ -z "$LANIP" ]; then LANIP="127.0.0.1"; fi
OK=0
for i in 1 2 3 4 5 6; do
  sleep 1
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then OK=1; break; fi
  elif "$PY" -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:$PORT/health',timeout=2)" >/dev/null 2>&1; then
    OK=1; break
  fi
done
# --- 7. 若是通过 curl | bash 装的，从本机服务把脚本本体存一份下来 ---
if [ ! -f /opt/roceos-dash/install.sh ]; then
  if command -v curl >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:$PORT/install.sh" -o /opt/roceos-dash/install.sh >/dev/null 2>&1 \
      && chmod +x /opt/roceos-dash/install.sh || true
  elif command -v wget >/dev/null 2>&1; then
    wget -q -O /opt/roceos-dash/install.sh "http://127.0.0.1:$PORT/install.sh" >/dev/null 2>&1 \
      && chmod +x /opt/roceos-dash/install.sh || true
  fi
fi
echo "--------------------------------------------------------------"
if [ "$OK" = "1" ]; then
  echo " 安装完成，看板已就绪"
  echo " 访问地址： http://$LANIP:$PORT"
  echo "           （同一局域网内任意设备打开 http://<本机IP>:$PORT 即可）"
else
  echo " 文件已部署，但健康检查未通过，请查看日志： journalctl -u roceos-dash -n 50"
fi
echo " 查看状态： systemctl status roceos-dash"
echo " 实时日志： journalctl -u roceos-dash -f"
echo " 换端口重： ROCEOS_DASH_PORT=8080 bash /opt/roceos-dash/install.sh"
echo " 卸载：     systemctl disable --now roceos-dash && rm -rf /opt/roceos-dash /etc/systemd/system/roceos-dash.service"
echo "--------------------------------------------------------------"
"""
    return tmpl.replace("__PORT__", str(PORT)).replace("__B64__", b64_lines)

class Handler(BaseHTTPRequestHandler):
    server_version = "roceos-dash"

    def log_message(self, fmt, *args):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html", "/dash", "/api/stats"):
            # 有人在看：更新活跃时间戳并唤醒采样线程（省电模式下立刻恢复 1 秒精度）
            LAST_REQ[0] = time.time()
            WAKE.set()
        if path in ("/", "/index.html", "/dash"):
            self._send(HTML)
        elif path == "/api/stats":
            try:
                data = collect_all()
                self._send(json.dumps(data, ensure_ascii=False), "application/json; charset=utf-8")
            except Exception as e:
                self._send(json.dumps({"error": str(e)}, ensure_ascii=False),
                           "application/json; charset=utf-8", 500)
        elif path in ("/install.sh", "/deploy.sh"):
            self._send(build_installer(), "text/plain; charset=utf-8")
        elif path == "/health":
            self._send("ok", "text/plain; charset=utf-8")
        else:
            self._send("404", "text/plain; charset=utf-8", 404)


def main():
    global ACTUAL_PORT
    SAMPLER.start()
    # 抓包只服务于"每台设备的实时速率"，本版已移除该模块 → 默认不开，省掉整条抓包链路
    if os.environ.get("ROCEOS_DASH_SNIFF", "0") != "0":
        try:
            SNIFFER.start()             # 需要 root；无权限时静默失败，退化为其它速率来源
        except Exception:
            pass
    else:
        SNIFFER.active = False
    time.sleep(1.2)
    srv = None
    for p in range(PORT, PORT + 11):        # 端口被占用就顺延，避免直接起不来
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", p), Handler)
            ACTUAL_PORT = p
            break
        except OSError as e:
            sys.stderr.write("port %d unavailable (%s), trying next\n" % (p, e))
    if srv is None:
        sys.stderr.write("no free port in %d-%d, exit\n" % (PORT, PORT + 10))
        sys.exit(1)
    sys.stderr.write("roceos-dash listening on 0.0.0.0:%d\n" % ACTUAL_PORT)
    sys.stderr.flush()
    srv.serve_forever()


if __name__ == "__main__":
    main()
