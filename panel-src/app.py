#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zzj3 服务器监控面板 - 零依赖后端采集
仅使用 Python 3 标准库，读 /proc 采集指标，对外提供 / 页面 与 /api/status JSON。
"""
import json
import os
import platform
import socket
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(BASE_DIR, "index.html")
PORT = int(os.environ.get("PANEL_PORT", "8899"))
# 非空时启用访问密钥：?token=xxx 或请求头 X-Panel-Token
PANEL_TOKEN = os.environ.get("PANEL_TOKEN", "").strip()

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# ---- 采样状态（两次采样之间算速率/占比）----
_state = {"cpu": None, "net": None, "procs": {}, "ts": None}


def _read_meminfo():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split(":")
            if len(parts) == 2:
                info[parts[0]] = int(parts[1].strip().split()[0]) * 1024
    return info


def _read_cpu():
    with open("/proc/stat") as f:
        vals = list(map(int, f.readline().split()[1:]))
    idle = vals[3] + vals[4]  # idle + iowait
    return idle, sum(vals)


def _cpu_percent():
    idle, total = _read_cpu()
    prev = _state["cpu"]
    _state["cpu"] = (idle, total)
    if not prev:
        return 0.0
    p_idle, p_total = prev
    dt_total = total - p_total
    dt_idle = idle - p_idle
    if dt_total <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1 - dt_idle / dt_total) * 100)), 1)


def collect_cpu():
    load = open("/proc/loadavg").read().split()
    return {
        "cores": os.cpu_count() or 1,
        "percent": _cpu_percent(),
        "load": [float(load[0]), float(load[1]), float(load[2])],
    }


def collect_mem():
    m = _read_meminfo()
    total = m.get("MemTotal", 0)
    avail = m.get("MemAvailable", 0)
    used = total - avail
    swap_total = m.get("SwapTotal", 0)
    swap_free = m.get("SwapFree", 0)
    return {
        "total": total,
        "used": used,
        "available": avail,
        "buffers": m.get("Buffers", 0),
        "cached": m.get("Cached", 0),
        "percent": round(used / total * 100, 1) if total else 0.0,
        "swap_total": swap_total,
        "swap_used": swap_total - swap_free,
    }


def _partitions():
    """返回需要展示的真实磁盘挂载点（只保留常见块设备文件系统）。"""
    allow_fs = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs",
                "vfat", "ntfs", "fuse"}
    mounts = []
    with open("/proc/mounts") as f:
        for line in f:
            dev, mount, fs = line.split()[:3]
            if fs not in allow_fs or dev.startswith("loop"):
                continue
            if mount.startswith("/var/lib/docker"):
                continue
            mounts.append((dev, mount, fs))
    # 同一设备只保留根/最先出现的挂载点
    seen_dev, seen_mount, out = set(), set(), []
    for dev, mount, fs in mounts:
        if mount in seen_mount or dev in seen_dev:
            continue
        seen_dev.add(dev)
        seen_mount.add(mount)
        out.append((dev, mount, fs))
    return out


def collect_disk():
    parts = []
    for dev, mount, fs in _partitions():
        try:
            usage = os.statvfs(mount)
            total = usage.f_blocks * usage.f_frsize
            free = usage.f_bavail * usage.f_frsize
            used = total - free
            if total <= 0:
                continue
            parts.append({
                "device": dev,
                "mount": mount,
                "fs": fs,
                "total": total,
                "used": used,
                "free": free,
                "percent": round(used / total * 100, 1),
            })
        except OSError:
            continue
    parts.sort(key=lambda x: (x["mount"] != "/", x["mount"]))
    return parts


def collect_net():
    rx = tx = 0
    ifaces = []
    with open("/proc/net/dev") as f:
        next(f)
        for line in f:
            if ":" not in line:
                continue
            name, rest = line.split(":", 1)
            name = name.strip()
            if name == "lo":
                continue
            fields = rest.split()
            r, t = int(fields[0]), int(fields[8])
            rx += r
            tx += t
            ifaces.append({"name": name, "rx": r, "tx": t})
    now = time.time()
    prev = _state["net"]
    rx_rate = tx_rate = 0.0
    if prev:
        dt = now - prev[0]
        if dt > 0:
            rx_rate = max(0.0, (rx - prev[1]) / dt)
            tx_rate = max(0.0, (tx - prev[2]) / dt)
    _state["net"] = (now, rx, tx)
    return {
        "rx_total": rx,
        "tx_total": tx,
        "rx_rate": round(rx_rate, 1),
        "tx_rate": round(tx_rate, 1),
        "ifaces": ifaces,
    }


def _read_proc(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read().split()
        utime, stime = int(stat[13]), int(stat[14])
        with open(f"/proc/{pid}/status") as f:
            name, rss = "", 0
            for line in f:
                if line.startswith("Name:"):
                    name = line.split()[1]
                elif line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
        try:
            with open(f"/proc/{pid}/cmdline") as f:
                cmd = f.read().replace("\x00", " ").strip()
        except OSError:
            cmd = ""
        return {"pid": pid, "name": name, "cpu_ticks": utime + stime,
                "rss": rss, "cmd": cmd or name}
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
        return None


def collect_procs(limit=8):
    pids = [p for p in os.listdir("/proc") if p.isdigit()]
    now = time.time()
    prev = _state["procs"]
    cur = {}
    rows = []
    clk = CLK_TCK
    for p in pids:
        info = _read_proc(p)
        if not info:
            continue
        cur[info["pid"]] = (info["cpu_ticks"], now)
        old = prev.get(info["pid"])
        cpu = 0.0
        if old:
            dt_ticks = info["cpu_ticks"] - old[0]
            dt = (now - old[1]) * clk
            if dt > 0:
                cpu = round(dt_ticks / dt * 100 / (os.cpu_count() or 1), 1)
        rows.append({
            "pid": info["pid"],
            "name": info["name"],
            "cpu": max(0.0, cpu),
            "rss": info["rss"],
            "cmd": info["cmd"][:120],
        })
    _state["procs"] = cur
    rows.sort(key=lambda x: x["cpu"], reverse=True)
    return rows[:limit]


def collect_docker():
    try:
        out = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True, timeout=8)
        if out.returncode != 0:
            return {"available": False, "containers": [], "error": out.stderr.strip()[:120]}
        containers = []
        for line in out.stdout.strip().splitlines():
            if not line:
                continue
            name, image, status, ports = (line.split("\t") + ["", "", "", ""])[:4]
            containers.append({"name": name, "image": image,
                               "status": status, "ports": ports})
        return {"available": True, "containers": containers}
    except FileNotFoundError:
        return {"available": False, "containers": [], "error": "docker 未安装"}


def collect_system():
    uptime_sec = float(open("/proc/uptime").read().split()[0])
    os_name = platform.platform()
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    os_name = line.split("=", 1)[1].strip().strip('"')
                    break
    except OSError:
        pass
    return {
        "hostname": socket.gethostname(),
        "os": os_name,
        "kernel": platform.release(),
        "arch": platform.machine(),
        "uptime": round(uptime_sec, 1),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def snapshot():
    return {
        "ts": time.time(),
        "system": collect_system(),
        "cpu": collect_cpu(),
        "mem": collect_mem(),
        "disk": collect_disk(),
        "net": collect_net(),
        "procs": collect_procs(),
        "docker": collect_docker(),
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _auth_ok(self):
        if not PANEL_TOKEN:
            return True
        q = parse_qs(urlparse(self.path).query)
        token = q.get("token", [""])[0] or self.headers.get("X-Panel-Token", "")
        return token == PANEL_TOKEN

    def do_GET(self):
        path = urlparse(self.path).path
        if not self._auth_ok():
            self._send(401, json.dumps({"error": "unauthorized"}))
            return
        if path == "/api/status":
            self._send(200, json.dumps(snapshot(), ensure_ascii=False))
        elif path in ("/", "/index.html"):
            try:
                with open(INDEX_HTML, encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html not found")
        else:
            self._send(404, json.dumps({"error": "not found"}))


if __name__ == "__main__":
    # 首次预热采样，避免首轮占比/速率为 0 失真
    snapshot()
    time.sleep(0.4)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"monitor-panel listening on 0.0.0.0:{PORT}", flush=True)
    httpd.serve_forever()
