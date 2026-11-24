#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zzj3 服务器监控面板 - 增强版后端（零第三方依赖，仅 Python 标准库）
采集 /proc 指标，SQLite 持久化历史趋势，提供监控 / 告警 / 进程与容器管理接口。
路由：
  GET  /                      面板页面
  GET  /api/status            实时快照 + 告警
  GET  /api/history?range=秒  历史时序
  GET  /api/procs?search=关键字  进程列表（可搜索）
  GET  /api/services          运行中的系统服务
  POST /api/proc/kill?pid=    结束进程（SIGTERM）
  POST /api/docker/restart?name=  重启容器
  POST /api/docker/stop?name=     停止容器
"""
import json
import os
import platform
import signal
import socket
import sqlite3
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INDEX_HTML = os.path.join(BASE_DIR, "index.html")
DB_PATH = os.path.join(BASE_DIR, "monitor.db")
PORT = int(os.environ.get("PANEL_PORT", "8899"))
PANEL_TOKEN = os.environ.get("PANEL_TOKEN", "").strip()
HISTORY_KEEP_SEC = 24 * 3600

CLK_TCK = os.sysconf("SC_CLK_TCK")

# ---- 采样状态（两次采样之间算速率/占比）----
_state = {"cpu": None, "net": None, "diskio": None, "procs": {}, "ts": None}


# ------------------------- 基础采集 -------------------------

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
    idle = vals[3] + vals[4]
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
        "swap_percent": round((swap_total - swap_free) / swap_total * 100, 1) if swap_total else 0.0,
    }


def _partitions():
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
                "device": dev, "mount": mount, "fs": fs,
                "total": total, "used": used, "free": free,
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
        "rx_total": rx, "tx_total": tx,
        "rx_rate": round(rx_rate, 1), "tx_rate": round(tx_rate, 1),
        "ifaces": ifaces,
    }


def collect_diskio():
    rd = wr = 0
    try:
        with open("/proc/diskstats") as f:
            for line in f:
                x = line.split()
                name = x[2]
                if name.startswith(("loop", "ram", "dm-")):
                    continue
                rd += int(x[5])
                wr += int(x[9])
    except FileNotFoundError:
        pass
    now = time.time()
    prev = _state["diskio"]
    rd_rate = wr_rate = 0.0
    if prev:
        dt = now - prev[0]
        if dt > 0:
            rd_rate = max(0.0, (rd - prev[1]) * 512 / dt)
            wr_rate = max(0.0, (wr - prev[2]) * 512 / dt)
    _state["diskio"] = (now, rd, wr)
    return {
        "read_rate": round(rd_rate, 1), "write_rate": round(wr_rate, 1),
        "read_total": rd * 512, "write_total": wr * 512,
    }


def collect_connections():
    established = listen = total = 0
    for path in ("/proc/net/tcp", "/proc/net/tcp6", "/proc/net/udp", "/proc/net/udp6"):
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    st = line.split()[3]
                    total += 1
                    if st == "01":
                        established += 1
                    elif st == "0A":
                        listen += 1
        except FileNotFoundError:
            pass
    return {"established": established, "listen": listen, "total": total}


def collect_ports():
    ports = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path) as f:
                next(f)
                for line in f:
                    x = line.split()
                    if x[3] == "0A":
                        ports.add(int(x[1].split(":")[1], 16))
        except FileNotFoundError:
            pass
    return sorted(ports)


def collect_services():
    try:
        out = subprocess.run(
            ["systemctl", "list-units", "--type=service", "--state=running",
             "--no-pager", "--no-legend"],
            capture_output=True, text=True, timeout=8)
        svcs = []
        for line in out.stdout.splitlines():
            x = line.split(None, 4)
            if len(x) >= 5:
                svcs.append({"name": x[0], "active": x[3], "desc": x[4].strip()})
        return svcs[:40]
    except Exception:
        return []


def collect_temp():
    zones = []
    base = "/sys/class/thermal"
    if os.path.isdir(base):
        for z in sorted(os.listdir(base)):
            if z.startswith("thermal_zone"):
                try:
                    t = int(open(f"{base}/{z}/temp").read().strip()) / 1000
                    typ = open(f"{base}/{z}/type").read().strip()
                    zones.append({"zone": typ, "temp": round(t, 1)})
                except (OSError, ValueError):
                    pass
    return zones


def _read_proc(pid):
    try:
        with open(f"/proc/{pid}/stat") as f:
            stat = f.read().split()
        utime, stime = int(stat[13]), int(stat[14])
        with open(f"/proc/{pid}/status") as f:
            name, rss, state = "", 0, ""
            for line in f:
                if line.startswith("Name:"):
                    name = line.split()[1]
                elif line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                elif line.startswith("State:"):
                    state = line.split()[1]
        try:
            with open(f"/proc/{pid}/cmdline") as f:
                cmd = f.read().replace("\x00", " ").strip()
        except OSError:
            cmd = ""
        return {"pid": pid, "name": name, "state": state,
                "cpu_ticks": utime + stime, "rss": rss, "cmd": cmd or name}
    except (FileNotFoundError, ProcessLookupError, ValueError, IndexError):
        return None


def collect_procs(limit=None, search=""):
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
            "pid": info["pid"], "name": info["name"], "state": info["state"],
            "cpu": max(0.0, cpu), "rss": info["rss"], "cmd": info["cmd"][:160],
        })
    _state["procs"] = cur
    rows.sort(key=lambda x: x["cpu"], reverse=True)
    if search:
        s = search.lower()
        rows = [r for r in rows if s in r["name"].lower() or s in r["cmd"].lower()]
    return rows[:limit] if limit else rows


def collect_docker():
    try:
        out = subprocess.run(
            ["docker", "ps", "-a", "--format",
             "{{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}\t{{.State}}"],
            capture_output=True, text=True, timeout=8)
        if out.returncode != 0:
            return {"available": False, "containers": [], "error": out.stderr.strip()[:120]}
        containers = []
        for line in out.stdout.strip().splitlines():
            if not line:
                continue
            name, image, status, ports, state = (line.split("\t") + ["", "", "", "", ""])[:5]
            containers.append({"name": name, "image": image, "status": status,
                               "state": state, "ports": ports})
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


# ------------------------- 告警 -------------------------

def _level(pct, warn, bad):
    return "bad" if pct >= bad else ("warn" if pct >= warn else "ok")


def build_alerts(cpu, mem, disk, swap, load, cores):
    alerts = []
    lv = _level(cpu["percent"], 70, 85)
    if lv != "ok":
        alerts.append({"level": lv, "target": "CPU", "value": cpu["percent"],
                       "message": f"CPU 使用率 {cpu['percent']}%"})
    lv = _level(mem["percent"], 75, 90)
    if lv != "ok":
        alerts.append({"level": lv, "target": "内存", "value": mem["percent"],
                       "message": f"内存使用率 {mem['percent']}%"})
    if mem["swap_total"]:
        lv = _level(mem["swap_percent"], 60, 85)
        if lv != "ok":
            alerts.append({"level": lv, "target": "Swap", "value": mem["swap_percent"],
                           "message": f"Swap 使用率 {mem['swap_percent']}%"})
    for d in disk:
        lv = _level(d["percent"], 75, 90)
        if lv != "ok":
            alerts.append({"level": lv, "target": f"磁盘 {d['mount']}", "value": d["percent"],
                           "message": f"{d['mount']} 已用 {d['percent']}%"})
    load1 = load[0]
    if load1 >= cores * 1.5:
        alerts.append({"level": "bad", "target": "负载", "value": load1,
                       "message": f"1 分钟负载 {load1}（{cores} 核）"})
    alerts.sort(key=lambda a: 0 if a["level"] == "bad" else 1)
    return alerts


# ------------------------- 历史持久化 -------------------------

def _db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS metrics(
        ts REAL, cpu REAL, mem REAL, swap REAL, rx REAL, tx REAL,
        disk REAL, load1 REAL)""")
    return conn


def record_history(snap):
    root_disk = next((d["percent"] for d in snap["disk"] if d["mount"] == "/"), 0.0)
    conn = _db()
    conn.execute(
        "INSERT INTO metrics VALUES(?,?,?,?,?,?,?,?)",
        (snap["ts"], snap["cpu"]["percent"], snap["mem"]["percent"],
         snap["mem"]["swap_percent"], snap["net"]["rx_rate"], snap["net"]["tx_rate"],
         root_disk, snap["cpu"]["load"][0]))
    conn.execute("DELETE FROM metrics WHERE ts < ?", (time.time() - HISTORY_KEEP_SEC,))
    conn.commit()
    conn.close()


def read_history(range_sec):
    conn = _db()
    rows = conn.execute(
        "SELECT ts,cpu,mem,swap,rx,tx,disk,load1 FROM metrics WHERE ts>=? ORDER BY ts",
        (time.time() - range_sec,)).fetchall()
    conn.close()
    return [{"ts": r[0], "cpu": r[1], "mem": r[2], "swap": r[3], "rx": r[4],
             "tx": r[5], "disk": r[6], "load1": r[7]} for r in rows]


# ------------------------- 快照 -------------------------

def snapshot():
    cpu = collect_cpu()
    mem = collect_mem()
    disk = collect_disk()
    net = collect_net()
    snap = {
        "ts": time.time(),
        "system": collect_system(),
        "cpu": cpu,
        "mem": mem,
        "disk": disk,
        "net": net,
        "diskio": collect_diskio(),
        "connections": collect_connections(),
        "ports": collect_ports(),
        "services": collect_services(),
        "temp": collect_temp(),
        "procs": collect_procs(limit=10),
        "docker": collect_docker(),
    }
    snap["alerts"] = build_alerts(cpu, mem, disk, mem["swap_percent"], cpu["load"], cpu["cores"])
    record_history(snap)
    return snap


# ------------------------- 写操作 -------------------------

def kill_pid(pid):
    try:
        os.kill(int(pid), signal.SIGTERM)
        return True, "ok"
    except (ValueError, ProcessLookupError, PermissionError) as e:
        return False, str(e)


def docker_action(action, name):
    if not name:
        return False, "缺少容器名"
    try:
        out = subprocess.run(["docker", action, name], capture_output=True,
                             text=True, timeout=12)
        if out.returncode == 0:
            return True, "ok"
        return False, out.stderr.strip()[:160] or "执行失败"
    except subprocess.TimeoutExpired:
        return False, "超时"


# ------------------------- HTTP 处理 -------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

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

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._auth_ok():
            self._json({"error": "unauthorized"}, 401)
            return
        q = parse_qs(parsed.query)
        if path == "/api/status":
            self._json(snapshot())
        elif path == "/api/history":
            rng = int(q.get("range", ["3600"])[0])
            rng = max(60, min(rng, HISTORY_KEEP_SEC))
            self._json({"range": rng, "points": read_history(rng)})
        elif path == "/api/procs":
            search = q.get("search", [""])[0]
            self._json({"procs": collect_procs(search=search)})
        elif path == "/api/services":
            self._json({"services": collect_services()})
        elif path in ("/", "/index.html"):
            try:
                with open(INDEX_HTML, encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except FileNotFoundError:
                self._send(404, "index.html not found")
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not self._auth_ok():
            self._json({"error": "unauthorized"}, 401)
            return
        q = parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            self.rfile.read(length)
        if path == "/api/proc/kill":
            pid = q.get("pid", [""])[0]
            ok, msg = kill_pid(pid)
            self._json({"ok": ok, "message": msg}, 200 if ok else 500)
        elif path == "/api/docker/restart":
            name = q.get("name", [""])[0]
            ok, msg = docker_action("restart", name)
            self._json({"ok": ok, "message": msg}, 200 if ok else 500)
        elif path == "/api/docker/stop":
            name = q.get("name", [""])[0]
            ok, msg = docker_action("stop", name)
            self._json({"ok": ok, "message": msg}, 200 if ok else 500)
        else:
            self._json({"error": "not found"}, 404)


if __name__ == "__main__":
    snapshot()  # 预热采样
    time.sleep(0.4)
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"monitor-panel listening on 0.0.0.0:{PORT}", flush=True)
    httpd.serve_forever()
