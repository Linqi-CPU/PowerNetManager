# -*- coding: utf-8 -*-
"""
电源计划与网络优化工具 (Power Plan & Network Manager)
- 电源：计划切换/新建/删除、显示与睡眠超时、省电/平衡/高性能一键预设、睡眠/休眠/锁定
- 网络：网卡状态、IP/DNS/网关/公网IP、Ping 测速、DNS 切换、TCP 优化、Winsock/TCP 重置
仅依赖 Python 标准库（tkinter）。需要 Windows 8+ / 中文版或英文版系统命令均可解析。
"""
import ctypes
import json
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, simpledialog

APP_TITLE = "电源计划与网络优化工具"
CREATE_NO_WINDOW = 0x08000000
GUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                     r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

BUILTIN_GUIDS = {
    "381b4222-f694-41f0-9685-ff5bb260df2e",  # 平衡 Balanced
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",  # 高性能 High performance
    "a1841308-3541-4fab-bc81-f71556f20b4a",  # 节能 Power saver
    "e9a42b02-d5df-448d-aa00-03f14749eb61",  # 节能器(新)
}


# ===================== 命令执行基础层 =====================

def decode_bytes(b: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def run_cmd(args, timeout=40):
    """执行命令，返回 (returncode, 合并输出文本)。不弹黑窗。"""
    try:
        p = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        out = decode_bytes(p.stdout) + decode_bytes(p.stderr)
        return p.returncode, out.strip()
    except FileNotFoundError:
        return -1, f"找不到命令: {args[0]}"
    except subprocess.TimeoutExpired:
        return -1, f"命令超时 ({timeout}s): {' '.join(args)}"
    except Exception as e:
        return -1, f"执行失败: {type(e).__name__}: {e}"


def run_ps(script: str, timeout=30):
    """执行 PowerShell 脚本，返回 (returncode, 输出文本)。"""
    return run_cmd(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        timeout=timeout,
    )


def needs_elevation(text: str) -> bool:
    t = text.lower()
    return ("提升" in text) or ("elevat" in t) or ("需要管理员" in text) \
        or ("请求的操作需要" in text) or ("run as administrator" in t)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ===================== 电源计划 =====================

def list_plans():
    """返回 [(guid, name, active_bool)]"""
    code, out = run_cmd(["powercfg", "/list"])
    plans = []
    for line in out.splitlines():
        m = GUID_RE.search(line)
        if not m:
            continue
        name_m = re.search(r"\(([^()]*)\)", line)
        name = name_m.group(1).strip() if name_m else "(未命名)"
        active = line.rstrip().endswith("*")
        plans.append((m.group(0).lower(), name, active))
    return plans


def active_plan_guid():
    code, out = run_cmd(["powercfg", "/getactivescheme"])
    m = GUID_RE.search(out or "")
    return m.group(0).lower() if m else None


def query_idle(setting_alias):
    """查询当前计划某设置的 AC/DC 秒数，返回 (ac_seconds, dc_seconds) 或 None"""
    code, out = run_cmd(["powercfg", "/query", "SCHEME_CURRENT",
                         "SUB_VIDEO" if setting_alias == "VIDEOIDLE" else "SUB_SLEEP",
                         setting_alias])
    vals = re.findall(r"(?:0x[0-9a-fA-F]+)?[^\n]*?索引\s*:\s*(0x[0-9a-fA-F]+)", out)
    # 英文系统回退
    if not vals:
        vals = re.findall(r"Index:\s*(0x[0-9a-fA-F]+)", out)
    if len(vals) >= 2:
        return int(vals[0], 16), int(vals[1], 16)
    return None


def set_timeouts(ac_display_s, dc_display_s, ac_sleep_s, dc_sleep_s, log=print):
    """设置当前计划的显示关闭/睡眠超时（秒），并立即生效。"""
    pairs = [
        ("SUB_VIDEO", "VIDEOIDLE", ac_display_s, dc_display_s, "关闭显示器"),
        ("SUB_SLEEP", "STANDBYIDLE", ac_sleep_s, dc_sleep_s, "电脑睡眠"),
    ]
    ok_all = True
    for subgroup, setting, ac_v, dc_v, label in pairs:
        for flag, val in (("/setacvalueindex", ac_v), ("/setdcvalueindex", dc_v)):
            code, out = run_cmd(["powercfg", flag, "SCHEME_CURRENT",
                                 subgroup, setting, str(int(val))])
            if code != 0:
                ok_all = False
                log(f"  设置{label}失败: {out}", "error")
    code, out = run_cmd(["powercfg", "/setactive", "SCHEME_CURRENT"])
    if code != 0:
        ok_all = False
        log(f"  激活设置失败: {out}", "error")
    return ok_all


def activate_plan(guid):
    return run_cmd(["powercfg", "/setactive", guid])


def create_plan(name):
    """基于平衡计划复制一个新计划，返回新 GUID 或 None"""
    code, out = run_cmd(["powercfg", "/duplicatescheme",
                         "381b4222-f694-41f0-9685-ff5bb260df2e"])
    guids = GUID_RE.findall(out or "")
    if code != 0 or not guids:
        return None, out
    new_guid = guids[0]
    code2, out2 = run_cmd(["powercfg", "/changename", new_guid, name])
    return new_guid, (out + "\n" + out2).strip()


def delete_plan(guid):
    return run_cmd(["powercfg", "/delete", guid])


def battery_info():
    """返回 dict(percent, status_code) 或 None（台式机无电池）"""
    ps = ("Get-CimInstance Win32_Battery | "
          "Select-Object EstimatedChargeRemaining,BatteryStatus | "
          "ConvertTo-Json -Compress")
    code, out = run_ps(ps)
    out = (out or "").strip()
    if not out:
        return None
    try:
        data = json.loads(out)
        if isinstance(data, list):
            data = data[0]
        return {
            "percent": int(data.get("EstimatedChargeRemaining", -1)),
            "status": int(data.get("BatteryStatus", 0)),
        }
    except Exception:
        return None


def power_action(action):
    """睡眠/休眠/锁定/关机/重启"""
    if action == "lock":
        ctypes.windll.user32.LockWorkStation()
        return 0, ""
    if action in ("sleep", "hibernate"):
        powrprof = ctypes.windll.powrprof
        ok = powrprof.SetSuspendState(1 if action == "hibernate" else 0, 0, 0)
        return (0 if ok else 1), "" if ok else "SetSuspendState 调用失败（休眠可能未启用：powercfg /hibernate on）"
    if action == "shutdown":
        return run_cmd(["shutdown", "/s", "/t", "0"])
    if action == "restart":
        return run_cmd(["shutdown", "/r", "/t", "0"])
    return 1, "未知操作"


# ===================== 网络 =====================

WIFI_SUBGROUP = "19cbb8fa-5279-450e-9fac-8a3d5fedd0c1"   # 无线适配器设置
WIFI_SETTING = "12bbebe6-58d6-4636-95bb-3217ef867c1a"    # 节能模式
WIFI_MODE_NAMES = ["最高性能", "低节能", "中等节能", "最高节能"]


def wifi_power_mode():
    """当前计划无线适配器节能模式 (ac_idx, dc_idx)，取值 0-3；失败返回 None"""
    code, out = run_cmd(["powercfg", "/query", "SCHEME_CURRENT",
                         WIFI_SUBGROUP, WIFI_SETTING])
    vals = re.findall(r"(?:0x[0-9a-fA-F]+)?[^\n]*?索引\s*:\s*(0x[0-9a-fA-F]+)", out)
    if not vals:
        vals = re.findall(r"Index:\s*(0x[0-9a-fA-F]+)", out)
    if len(vals) >= 2:
        return int(vals[0], 16), int(vals[1], 16)
    return None


def set_wifi_power_mode(ac_idx, dc_idx):
    """设置当前计划无线适配器节能等级（0最高性能 ~ 3最高节能），立即生效"""
    ok_all, text = True, ""
    for flag, val in (("/setacvalueindex", ac_idx), ("/setdcvalueindex", dc_idx)):
        code, out = run_cmd(["powercfg", flag, "SCHEME_CURRENT",
                             WIFI_SUBGROUP, WIFI_SETTING, str(int(val))])
        if code != 0:
            ok_all, text = False, out
    code, out = run_cmd(["powercfg", "/setactive", "SCHEME_CURRENT"])
    if code != 0:
        ok_all, text = False, out
    return ok_all, text


def nic_power_management(ifname):
    """设备级网卡电源能力。驱动不支持时返回 None。
    返回 dict(turnoff, magic, pattern)，值为 Enabled/Disabled/Unsupported"""
    ps = (f"try {{ Get-NetAdapterPowerManagement -Name '{ifname}' -ErrorAction Stop | "
          "Select-Object AllowComputerToTurnOffDevice,WakeOnMagicPacket,WakeOnPattern | "
          "ConvertTo-Json -Compress } catch { 'UNSUPPORTED' }")
    code, out = run_ps(ps)
    out = (out or "").strip()
    if not out or "UNSUPPORTED" in out:
        return None
    try:
        d = json.loads(out)
        return {
            "turnoff": str(d.get("AllowComputerToTurnOffDevice", "Unsupported")),
            "magic": str(d.get("WakeOnMagicPacket", "Unsupported")),
            "pattern": str(d.get("WakeOnPattern", "Unsupported")),
        }
    except Exception:
        return None


def set_nic_power_management(ifname, turnoff=None, magic=None, pattern=None):
    """设备级设置，参数传 'Enabled'/'Disabled'。需管理员。返回 (ok, msg)"""
    logs = []
    for param, val in (("AllowComputerToTurnOffDevice", turnoff),
                       ("WakeOnMagicPacket", magic),
                       ("WakeOnPattern", pattern)):
        if val is None:
            continue
        ps = (f"Set-NetAdapterPowerManagement -Name '{ifname}' -{param} {val} "
              "-Confirm:$false -ErrorAction Stop")
        code, out = run_ps(ps)
        if code != 0:
            return False, f"{param} 设置失败: {out.strip()[:200]}"
        logs.append(param)
    return True, ", ".join(logs)


def net_adapters():
    """返回 [dict(Name,Status,LinkSpeed,Mac,Desc)]"""
    ps = "Get-NetAdapter | Select-Object Name,Status,LinkSpeed,MacAddress,InterfaceDescription | ConvertTo-Json -Compress"
    code, out = run_ps(ps)
    if code != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
        return [{
            "name": d.get("Name", ""),
            "status": d.get("Status", ""),
            "speed": d.get("LinkSpeed", "") or "-",
            "mac": (d.get("MacAddress") or "").replace("-", ":"),
            "desc": d.get("InterfaceDescription", ""),
        } for d in data]
    except Exception:
        return []


def ip_configuration():
    """返回 [(ifname, ipv4, prefix_len, gateway, dns_list)]（含断开接口，IP 可能为空）"""
    ps = (
        "Get-NetIPConfiguration | ForEach-Object { "
        "$ip = Get-NetIPAddress -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 "
        "-ErrorAction SilentlyContinue | Where-Object { $_.IPAddress -notlike '169.254*' } "
        "| Select-Object -First 1; "
        "$dns = (Get-DnsClientServerAddress -InterfaceIndex $_.InterfaceIndex "
        "-AddressFamily IPv4 -ErrorAction SilentlyContinue).ServerAddresses -join ','; "
        "[pscustomobject]@{ Name=$_.InterfaceAlias; IP=$ip.IPAddress; "
        "Prefix=$ip.PrefixLength; GW=$_.IPv4DefaultGateway.NextHop; DNS=$dns } "
        "} | ConvertTo-Json -Compress"
    )
    code, out = run_ps(ps, timeout=25)
    if code != 0 or not (out or "").strip():
        return []
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            data = [data]
    except Exception:
        return []
    result = []
    for d in data:
        dns_raw = d.get("DNS") or ""
        dns = [s.strip() for s in dns_raw.split(",") if s.strip()]
        result.append((
            d.get("Name", "") or "",
            d.get("IP") or "",
            str(d.get("Prefix") or ""),
            d.get("GW") or "",
            dns,
        ))
    return result


def ping_host(target, count=4):
    """返回 dict(ok, loss_pct, min, max, avg, ttl, raw) 或 None"""
    code, out = run_cmd(["ping", "-n", str(int(count)), "-w", "2000", target], timeout=count*3+8)
    if out is None:
        return None
    loss_m = re.search(r"(\d+)\s*%\s*(?:丢失|loss)", out, re.I)
    loss = int(loss_m.group(1)) if loss_m else 100
    ttl_m = re.search(r"TTL[=:]\s*(\d+)", out, re.I)
    stats = re.search(
        r"(?:最短|Minimum)\s*=\s*(\d+)ms.*?(?:最长|Maximum)\s*=\s*(\d+)ms.*?"
        r"(?:平均|Average)\s*=\s*(\d+)ms", out, re.S | re.I)
    return {
        "ok": loss < 100,
        "loss": loss,
        "min": int(stats.group(1)) if stats else None,
        "max": int(stats.group(2)) if stats else None,
        "avg": int(stats.group(3)) if stats else None,
        "ttl": int(ttl_m.group(1)) if ttl_m else None,
        "raw": out,
    }


def public_ip():
    """返回 (展示文本, ip)。先 ipip（国内快且含地理位置），失败回退 ipify"""
    ps1 = ('try { (Invoke-WebRequest -Uri "https://myip.ipip.net" -TimeoutSec 6 '
           '-UseBasicParsing).Content } catch { "" }')
    code, out = run_ps(ps1, timeout=12)
    out = (out or "").strip()
    m = re.search(r"\d{1,3}(?:\.\d{1,3}){3}", out)
    if m:
        return re.sub(r"\s+", " ", out), m.group(0)
    ps2 = ('try { "IP: " + (Invoke-RestMethod -Uri "https://api.ipify.org" '
           '-TimeoutSec 6) } catch { "" }')
    code, out = run_ps(ps2, timeout=12)
    out = (out or "").strip()
    m = re.search(r"\d{1,3}(?:\.\d{1,3}){3}", out)
    return (out or "查询失败（可能无网络或被防火墙拦截）"), (m.group(0) if m else None)


def tcp_global_text():
    code, out = run_cmd(["netsh", "interface", "tcp", "show", "global"])
    return out


def flush_dns():
    return run_cmd(["ipconfig", "/flushdns"])


def set_dns(ifname, preset):
    """preset: 'dhcp' 或 (primary, secondary)"""
    if preset == "dhcp":
        return run_cmd(["netsh", "interface", "ipv4", "set", "dnsservers",
                        f"name={ifname}", "source=dhcp"])
    primary, secondary = preset
    code1, out1 = run_cmd(["netsh", "interface", "ipv4", "set", "dnsservers",
                           f"name={ifname}", "source=static",
                           f"address={primary}", "register=primary", "validate=no"])
    if code1 != 0:
        return code1, out1
    if secondary:
        code2, out2 = run_cmd(["netsh", "interface", "ipv4", "add", "dnsservers",
                               f"name={ifname}", f"address={secondary}",
                               "index=2", "validate=no"])
        return code2, (out1 + "\n" + out2).strip()
    return code1, out1


DNS_PRESETS = [
    ("自动获取 (DHCP)", "dhcp"),
    ("阿里 DNS  223.5.5.5 / 223.6.6.6", ("223.5.5.5", "223.6.6.6")),
    ("腾讯 DNS  119.29.29.29 / 182.254.116.116", ("119.29.29.29", "182.254.116.116")),
    ("114 DNS  114.114.114.114 / 114.114.115.115", ("114.114.114.114", "114.114.115.115")),
    ("Cloudflare  1.1.1.1 / 1.0.0.1", ("1.1.1.1", "1.0.0.1")),
    ("Google  8.8.8.8 / 8.8.4.4", ("8.8.8.8", "8.8.4.4")),
]

# (标签, 命令list, 需要管理员, 默认勾选, 说明)
TCP_TWEAKS = [
    ("flushdns", "刷新 DNS 解析缓存", ["ipconfig", "/flushdns"],
     False, True, "清除本机 DNS 缓存，立即生效，无风险"),
    ("autotune", "TCP 接收窗口自动调优 = normal",
     ["netsh", "interface", "tcp", "set", "global", "autotuninglevel=normal"],
     True, True, "让高带宽/高延迟连接获得更大接收窗口（下载更快）"),
    ("rss", "启用 RSS 接收端缩放",
     ["netsh", "interface", "tcp", "set", "global", "rss=enabled"],
     True, True, "多核心分摊网络中断处理"),
    ("ecn", "启用 ECN 显式拥塞通知",
     ["netsh", "interface", "tcp", "set", "global", "ecncapability=enabled"],
     True, False, "少数老旧路由器不兼容，遇到异常可改回 disabled"),
    ("rsc", "启用 RSC 接收段合并",
     ["netsh", "interface", "tcp", "set", "global", "rsc=enabled"],
     True, False, "降低 CPU 占用；部分 Wi-Fi 网卡反而建议关闭"),
    ("initialrto", "初始 RTO 降低到 300ms",
     ["netsh", "interface", "tcp", "set", "global", "initialrto=300"],
     True, False, "快速重传小包；不稳定网络可能增加重传"),
    ("arpcache", "清除 ARP 缓存",
     ["netsh", "interface", "ip", "delete", "arpcache"],
     True, False, "网关更换后排障用，普通情况无需操作"),
    ("dhcp", "释放并重新获取 DHCP 租约",
     ["__DHCP__"],
     True, False, "会短暂断网几秒；IP 异常/租约冲突时使用"),
]

# 快速电源模式：(显示AC, 显示DC, 睡眠AC, 睡眠DC, 计划名关键词列表, 无线节能AC, 无线节能DC)
POWER_MODES = [
    ("省电模式", 5, 3, 15, 10, ["节能", "power saver", "ASUS Recommended"], 3, 3),
    ("平衡模式", 10, 5, 30, 15, ["平衡", "balanced"], 1, 2),
    ("高性能模式", 15, 10, 0, 0, ["高性能", "high", "卓越", "ultimate"], 0, 0),
]


# ===================== GUI =====================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x780")
        self.minsize(880, 640)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.msg_q = queue.Queue()
        self.busy = False
        self._closing = False

        self._setup_style()
        self._build_ui()
        self._poll_queue()
        # 初始加载
        self.refresh_plans()
        self.refresh_timeouts()
        self.refresh_battery()
        self.refresh_adapters()
        self.refresh_ip_info()
        self._refresh_tcp_text()
        self.after(800, self.refresh_wifi_mode)
        self.after(60_000, self._periodic)

    # ---------- 样式/布局 ----------

    def _setup_style(self):
        try:
            self.tk.call("tk", "scaling", 1.2)
        except tk.TclError:
            pass
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        font = ("Microsoft YaHei UI", 10)
        style.configure(".", font=font)
        style.configure("TButton", padding=4)
        style.configure("Danger.TButton", foreground="#c62828")
        style.configure("Accent.TButton", foreground="#0d47a1")
        style.configure("Treeview", rowheight=24, font=font)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TLabelFrame.Label", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_ui(self):
        # 顶部权限栏
        top = ttk.Frame(self, padding=(10, 6))
        top.pack(fill=tk.X)
        self.admin_var = tk.StringVar()
        ttk.Label(top, textvariable=self.admin_var, font=("Microsoft YaHei UI", 9)).pack(side=tk.LEFT)
        ttk.Button(top, text="以管理员身份重启", command=self.relaunch_elevated).pack(side=tk.RIGHT)
        ttk.Button(top, text="关于", command=self._about, width=8).pack(side=tk.RIGHT, padx=6)
        self._render_admin_badge()

        # 上下分割：Notebook + 日志
        pane = ttk.Panedwindow(self, orient=tk.VERTICAL)
        pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 4))

        nb = ttk.Notebook(pane)
        pane.add(nb, weight=3)
        self.tab_power = ttk.Frame(nb)
        self.tab_net = ttk.Frame(nb)
        nb.add(self.tab_power, text="  电源计划  ")
        nb.add(self.tab_net, text="  网络优化  ")
        self._build_power_tab()
        self._build_net_tab()

        log_frame = ttk.LabelFrame(pane, text="操作日志", padding=4)
        pane.add(log_frame, weight=1)
        self.log_text = scrolledtext.ScrolledText(
            log_frame, height=8, font=("Consolas", 9), wrap=tk.WORD,
            state=tk.DISABLED, background="#fafafa")
        self.log_text.pack(fill=tk.BOTH, expand=True)
        for tag, color in (("info", "#37474f"), ("cmd", "#1565c0"),
                           ("success", "#2e7d32"), ("error", "#c62828"),
                           ("warning", "#ef6c00")):
            self.log_text.tag_configure(tag, foreground=color)

        # 底部状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W,
                  padding=(8, 3)).pack(fill=tk.X, side=tk.BOTTOM)

    def _build_power_tab(self):
        tab = self.tab_power
        tab.columnconfigure(0, weight=1, uniform="p")
        tab.columnconfigure(1, weight=1, uniform="p")
        tab.rowconfigure(0, weight=1)

        # 左：计划列表
        lf = ttk.LabelFrame(tab, text="电源计划", padding=8)
        lf.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        lf.rowconfigure(0, weight=1)
        lf.columnconfigure(0, weight=1)
        cols = ("name", "guid", "state")
        self.plan_tree = ttk.Treeview(lf, columns=cols, show="headings", selectmode="browse")
        self.plan_tree.heading("name", text="名称")
        self.plan_tree.heading("guid", text="GUID")
        self.plan_tree.heading("state", text="状态")
        self.plan_tree.column("name", width=150, anchor=tk.W)
        self.plan_tree.column("guid", width=230, anchor=tk.W)
        self.plan_tree.column("state", width=70, anchor=tk.CENTER)
        self.plan_tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(lf, orient=tk.VERTICAL, command=self.plan_tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.plan_tree.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(lf)
        btns.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(btns, text="设为活动计划", style="Accent.TButton",
                   command=self.activate_selected_plan).pack(side=tk.LEFT)
        ttk.Button(btns, text="新建…", command=self.create_new_plan).pack(side=tk.LEFT, padx=4)
        ttk.Button(btns, text="删除", style="Danger.TButton",
                   command=self.delete_selected_plan).pack(side=tk.LEFT)
        ttk.Button(btns, text="刷新", command=self.refresh_plans).pack(side=tk.RIGHT)

        # 右：设置区（可滚动）
        right = self._scrollable(tab)
        right._holder.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)

        f1 = ttk.LabelFrame(right, text="快速模式（修改超时并尝试切换对应计划）", padding=8)
        f1.pack(fill=tk.X, pady=(0, 8))
        for i, (label, *_rest) in enumerate(POWER_MODES):
            ttk.Button(f1, text=label, width=14,
                       command=lambda idx=i: self.apply_power_mode(idx)
                       ).grid(row=0, column=i, padx=4)
        f1.columnconfigure("all", weight=1)

        f2 = ttk.LabelFrame(right, text="当前计划超时设置（分钟，0 = 从不）", padding=8)
        f2.pack(fill=tk.X, pady=(0, 8))
        self.var_ac_display = tk.StringVar(value="10")
        self.var_dc_display = tk.StringVar(value="5")
        self.var_ac_sleep = tk.StringVar(value="30")
        self.var_dc_sleep = tk.StringVar(value="15")
        rows = [
            ("接通电源 · 关闭显示器", self.var_ac_display),
            ("使用电池 · 关闭显示器", self.var_dc_display),
            ("接通电源 · 进入睡眠", self.var_ac_sleep),
            ("使用电池 · 进入睡眠", self.var_dc_sleep),
        ]
        for r, (label, var) in enumerate(rows):
            ttk.Label(f2, text=label).grid(row=r, column=0, sticky=tk.W, pady=3)
            ttk.Spinbox(f2, from_=0, to=600, width=6, textvariable=var).grid(
                row=r, column=1, sticky=tk.E, padx=(8, 4))
            ttk.Label(f2, text="分钟").grid(row=r, column=2, sticky=tk.W)
        f2.columnconfigure(0, weight=1)
        ttk.Button(f2, text="读取当前值", command=self.refresh_timeouts).grid(
            row=4, column=0, pady=(8, 0), sticky=tk.W)
        ttk.Button(f2, text="应用超时设置", style="Accent.TButton",
                   command=self.apply_timeouts).grid(row=4, column=1, columnspan=2,
                                                     pady=(8, 0), sticky=tk.E)

        f3 = ttk.LabelFrame(right, text="电池状态", padding=8)
        f3.pack(fill=tk.X, pady=(0, 8))
        self.battery_var = tk.StringVar(value="读取中…")
        ttk.Label(f3, textvariable=self.battery_var).pack(anchor=tk.W)
        ttk.Button(f3, text="刷新", command=self.refresh_battery).pack(anchor=tk.E)

        f4 = ttk.LabelFrame(right, text="电源操作", padding=8)
        f4.pack(fill=tk.X, pady=(0, 8))
        warn = ttk.Label(f4, text="重启/关机不会保存当前工作，请先保存文件。",
                         foreground="#c62828", font=("Microsoft YaHei UI", 9))
        warn.pack(anchor=tk.W, pady=(0, 6))
        row1 = ttk.Frame(f4)
        row1.pack(fill=tk.X)
        ttk.Button(row1, text="锁定", command=lambda: self.do_power_action("lock")).pack(side=tk.LEFT, padx=2)
        ttk.Button(row1, text="睡眠", command=lambda: self.do_power_action("sleep")).pack(side=tk.LEFT, padx=2)
        ttk.Button(row1, text="休眠", command=lambda: self.do_power_action("hibernate")).pack(side=tk.LEFT, padx=2)
        row2 = ttk.Frame(f4)
        row2.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(row2, text="重启", style="Danger.TButton",
                   command=lambda: self.do_power_action("restart")).pack(side=tk.LEFT, padx=2)
        ttk.Button(row2, text="关机", style="Danger.TButton",
                   command=lambda: self.do_power_action("shutdown")).pack(side=tk.LEFT, padx=2)

    def _build_net_tab(self):
        tab = self.tab_net
        holder_frame = ttk.Frame(tab)
        holder_frame.pack(fill=tk.BOTH, expand=True)
        body = self._scrollable(holder_frame)
        body._holder.pack(fill=tk.BOTH, expand=True)

        # —— 接口 + 信息 ——
        top = ttk.Frame(body)
        top.pack(fill=tk.X, pady=(8, 4))
        top.columnconfigure(0, weight=1, uniform="n")
        top.columnconfigure(1, weight=1, uniform="n")

        lf1 = ttk.LabelFrame(top, text="网络接口", padding=8)
        lf1.grid(row=0, column=0, sticky="nsew", padx=(8, 4))
        lf1.rowconfigure(0, weight=1)
        lf1.columnconfigure(0, weight=1)
        cols = ("name", "status", "speed", "mac")
        self.adapter_tree = ttk.Treeview(lf1, columns=cols, show="headings", height=5)
        self.adapter_tree.heading("name", text="接口")
        self.adapter_tree.heading("status", text="状态")
        self.adapter_tree.heading("speed", text="速率")
        self.adapter_tree.heading("mac", text="MAC")
        self.adapter_tree.column("name", width=110)
        self.adapter_tree.column("status", width=90, anchor=tk.CENTER)
        self.adapter_tree.column("speed", width=90, anchor=tk.CENTER)
        self.adapter_tree.column("mac", width=130)
        self.adapter_tree.grid(row=0, column=0, sticky="ew")
        ttk.Button(lf1, text="刷新接口", command=self.refresh_adapters).grid(
            row=1, column=0, sticky=tk.E, pady=(6, 0))
        self.adapter_tree.bind("<<TreeviewSelect>>", self._on_adapter_select)

        lf2 = ttk.LabelFrame(top, text="网络信息", padding=8)
        lf2.grid(row=0, column=1, sticky="nsew", padx=(4, 8))
        self.ip_info_text = tk.StringVar(value="读取中…")
        ttk.Label(lf2, textvariable=self.ip_info_text, justify=tk.LEFT,
                  font=("Consolas", 9)).pack(anchor=tk.W)
        ipb = ttk.Frame(lf2)
        ipb.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(ipb, text="刷新本机信息", command=self.refresh_ip_info).pack(side=tk.LEFT)
        ttk.Button(ipb, text="查询公网 IP", command=self.query_public_ip).pack(side=tk.LEFT, padx=6)

        # —— Ping + DNS ——
        mid = ttk.Frame(body)
        mid.pack(fill=tk.X, pady=4)
        mid.columnconfigure(0, weight=1, uniform="n")
        mid.columnconfigure(1, weight=1, uniform="n")

        lf3 = ttk.LabelFrame(mid, text="连接测试 (Ping)", padding=8)
        lf3.grid(row=0, column=0, sticky="nsew", padx=(8, 4))
        row = ttk.Frame(lf3)
        row.pack(fill=tk.X)
        ttk.Label(row, text="目标:").pack(side=tk.LEFT)
        self.ping_target = tk.StringVar(value="223.5.5.5  阿里DNS")
        ttk.Combobox(row, textvariable=self.ping_target, width=24, values=[
            "223.5.5.5  阿里DNS", "119.29.29.29  腾讯DNS",
            "114.114.114.114  114DNS", "www.baidu.com  百度",
            "www.microsoft.com  微软", "__gateway__  默认网关",
        ]).pack(side=tk.LEFT, padx=6)
        row2 = ttk.Frame(lf3)
        row2.pack(fill=tk.X, pady=(6, 0))
        ttk.Label(row2, text="次数:").pack(side=tk.LEFT)
        self.ping_count = tk.StringVar(value="4")
        ttk.Spinbox(row2, from_=1, to=20, width=5,
                    textvariable=self.ping_count).pack(side=tk.LEFT, padx=6)
        self.ping_btn = ttk.Button(row2, text="开始测试", style="Accent.TButton",
                                   command=self.do_ping)
        self.ping_btn.pack(side=tk.LEFT)
        self.ping_result = tk.StringVar(value="")
        ttk.Label(lf3, textvariable=self.ping_result, justify=tk.LEFT,
                  font=("Microsoft YaHei UI", 9)).pack(anchor=tk.W, pady=(8, 0))

        lf4 = ttk.LabelFrame(mid, text="DNS 设置（需要管理员）", padding=8)
        lf4.grid(row=0, column=1, sticky="nsew", padx=(4, 8))
        r = ttk.Frame(lf4)
        r.pack(fill=tk.X)
        ttk.Label(r, text="接口:").pack(side=tk.LEFT)
        self.dns_iface = tk.StringVar()
        self.dns_combo = ttk.Combobox(r, textvariable=self.dns_iface, width=22,
                                      state="readonly")
        self.dns_combo.pack(side=tk.LEFT, padx=6)
        self.dns_choice = tk.IntVar(value=0)
        for i, (label, _v) in enumerate(DNS_PRESETS):
            ttk.Radiobutton(lf4, text=label, variable=self.dns_choice,
                            value=i).pack(anchor=tk.W, pady=1)
        ttk.Button(lf4, text="应用 DNS 设置", command=self.apply_dns).pack(anchor=tk.E, pady=(6, 0))

        # —— 网卡电源管理 ——
        lfp = ttk.LabelFrame(body, text="网卡电源管理（省电 vs 网络稳定/唤醒）", padding=8)
        lfp.pack(fill=tk.X, padx=8, pady=4)
        # 计划级：无线适配器节能模式
        wf = ttk.Frame(lfp)
        wf.pack(fill=tk.X)
        ttk.Label(wf, text="无线适配器节能模式（当前电源计划）:").pack(side=tk.LEFT)
        self.var_wifi_ac = tk.StringVar()
        self.var_wifi_dc = tk.StringVar()
        mode_values = [f"{i} - {n}" for i, n in enumerate(WIFI_MODE_NAMES)]
        ttk.Label(wf, text="  交流").pack(side=tk.LEFT)
        ttk.Combobox(wf, textvariable=self.var_wifi_ac, values=mode_values,
                     width=12, state="readonly").pack(side=tk.LEFT, padx=(2, 10))
        ttk.Label(wf, text="电池").pack(side=tk.LEFT)
        ttk.Combobox(wf, textvariable=self.var_wifi_dc, values=mode_values,
                     width=12, state="readonly").pack(side=tk.LEFT, padx=2)
        wb = ttk.Frame(lfp)
        wb.pack(fill=tk.X, pady=(6, 0))
        ttk.Button(wb, text="读取当前无线节能", command=self.refresh_wifi_mode).pack(side=tk.LEFT)
        ttk.Button(wb, text="应用无线节能", style="Accent.TButton",
                   command=self.apply_wifi_mode).pack(side=tk.LEFT, padx=6)

        # 设备级：允许关闭设备省电 / 唤醒
        ttk.Separator(lfp).pack(fill=tk.X, pady=8)
        df = ttk.Frame(lfp)
        df.pack(fill=tk.X)
        ttk.Label(df, text="设备级电源（网卡驱动）:").pack(side=tk.LEFT)
        self.nic_pm_iface = tk.StringVar()
        self.nic_pm_combo = ttk.Combobox(df, textvariable=self.nic_pm_iface,
                                         width=18, state="readonly")
        self.nic_pm_combo.pack(side=tk.LEFT, padx=6)
        ttk.Button(df, text="读取", command=self.refresh_nic_pm).pack(side=tk.LEFT)
        self.nic_pm_state = tk.StringVar(value="尚未读取")
        ttk.Label(lfp, textvariable=self.nic_pm_state, foreground="#555555",
                  font=("Microsoft YaHei UI", 9), justify=tk.LEFT).pack(anchor=tk.W, pady=(6, 0))
        self.var_nic_turnoff = tk.StringVar(value="Disabled")
        self.var_nic_magic = tk.StringVar(value="Disabled")
        self.var_nic_pattern = tk.StringVar(value="Disabled")
        nb = ttk.Frame(lfp)
        nb.pack(fill=tk.X, pady=(4, 0))
        for i, (text, var) in enumerate((
                ("允许电脑关闭此设备以省电", self.var_nic_turnoff),
                ("魔术包唤醒 (WoL)", self.var_nic_magic),
                ("模式匹配唤醒", self.var_nic_pattern))):
            ttk.Label(nb, text=text).grid(row=i, column=0, sticky=tk.W, pady=2)
            ttk.Combobox(nb, textvariable=var, values=["Enabled", "Disabled"],
                         width=12, state="readonly").grid(row=i, column=1, padx=8, sticky=tk.W)
        ttk.Button(nb, text="应用设备电源设置（需管理员）", style="Accent.TButton",
                   command=self.apply_nic_pm).grid(row=3, column=0, columnspan=2,
                                                   sticky=tk.W, pady=(6, 0))
        ttk.Label(lfp, text="提示：关闭「允许电脑关闭设备省电」可避免休眠/待机后掉线；"
                            "笔记本断电使用时保持开启更省电。部分无线网卡驱动不暴露这些选项。",
                  foreground="#888888", wraplength=820, justify=tk.LEFT,
                  font=("Microsoft YaHei UI", 8)).pack(anchor=tk.W, pady=(6, 0))

        # —— TCP 优化 ——
        lf5 = ttk.LabelFrame(body, text="网络优化（勾选后一键执行）", padding=8)
        lf5.pack(fill=tk.X, padx=8, pady=4)
        self.tweak_vars = {}
        gridf = ttk.Frame(lf5)
        gridf.pack(fill=tk.X)
        for i, (key, label, _cmd, admin, default, desc) in enumerate(TCP_TWEAKS):
            var = tk.BooleanVar(value=default)
            self.tweak_vars[key] = var
            text = label + ("  （需管理员）" if admin else "")
            cb = ttk.Checkbutton(gridf, text=text, variable=var)
            cb.grid(row=i // 2, column=(i % 2) * 2, sticky=tk.W, padx=(0, 10), pady=2)
            ttk.Label(gridf, text=desc, foreground="#757575",
                      font=("Microsoft YaHei UI", 8)).grid(
                row=i // 2, column=(i % 2) * 2 + 1, sticky=tk.W, padx=(0, 16))
        gridf.columnconfigure(1, weight=1)
        gridf.columnconfigure(3, weight=1)
        brow = ttk.Frame(lf5)
        brow.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(brow, text="全选", width=6,
                   command=lambda: [v.set(True) for v in self.tweak_vars.values()]
                   ).pack(side=tk.LEFT)
        ttk.Button(brow, text="恢复默认勾选", width=12,
                   command=self._reset_tweak_checks).pack(side=tk.LEFT, padx=6)
        self.optimize_btn = ttk.Button(brow, text="执行优化", style="Accent.TButton",
                                       command=self.run_optimizations)
        self.optimize_btn.pack(side=tk.RIGHT)

        # —— TCP 当前参数 ——
        lf6 = ttk.LabelFrame(body, text="当前 TCP 全局参数", padding=8)
        lf6.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 4))
        tcpbar = ttk.Frame(lf6)
        tcpbar.pack(fill=tk.X)
        ttk.Button(tcpbar, text="刷新", command=self._refresh_tcp_text).pack(side=tk.RIGHT)
        self.tcp_text = scrolledtext.ScrolledText(lf6, height=10, font=("Consolas", 9),
                                                  wrap=tk.NONE)
        self.tcp_text.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

        # —— 危险区 ——
        lf7 = ttk.LabelFrame(body, text="高级重置（执行后需重启电脑）", padding=8)
        lf7.pack(fill=tk.X, padx=8, pady=(4, 10))
        ttk.Label(lf7, text="仅在网络严重异常、其他手段无效时使用；需要管理员权限。",
                  foreground="#c62828", font=("Microsoft YaHei UI", 9)).pack(anchor=tk.W)
        rb = ttk.Frame(lf7)
        rb.pack(anchor=tk.W, pady=(6, 0))
        ttk.Button(rb, text="重置 Winsock 目录", style="Danger.TButton",
                   command=lambda: self.dangerous_reset("winsock")).pack(side=tk.LEFT, padx=2)
        ttk.Button(rb, text="重置 TCP/IP 协议栈", style="Danger.TButton",
                   command=lambda: self.dangerous_reset("tcpip")).pack(side=tk.LEFT, padx=2)

    def _scrollable(self, parent):
        """在 parent（grid 管理）中放一个可滚动区域，返回可放置内容的内部 Frame"""
        holder = ttk.Frame(parent)
        canvas = tk.Canvas(holder, highlightthickness=0)
        vsb = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self._scroll_holders = getattr(self, "_scroll_holders", [])
        self._scroll_holders.append(holder)

        def _on_conf(event):
            canvas.configure(scrollregion=canvas.bbox("all"))
            canvas.itemconfigure(inner_id, width=event.width)

        def _on_wheel(event):
            canvas.yview_scroll(int(-event.delta / 120), "units")

        inner.bind("<Configure>", _on_conf)
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(inner_id, width=e.width))
        canvas.bind_all("<MouseWheel>", _on_wheel)
        # 触摸板/触屏滚动
        canvas.bind("<Enter>",
                    lambda _e: canvas.bind_all("<MouseWheel>", _on_wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))
        # 返回 holder 时让调用方无感：把 inner 暴露为返回值，但调用方需要 holder 布局
        inner._holder = holder
        inner._canvas = canvas
        return inner

    # ---------- 日志 / 队列 ----------

    def log(self, msg, tag="info"):
        self.msg_q.put(("log", msg, tag))

    def ui(self, fn):
        """线程安全：把 UI 更新函数投递回主线程执行（worker 线程禁止直接调 after）"""
        self.msg_q.put(("ui", fn))

    def _drain_log(self, msg, tag):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n", tag)
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def set_status(self, text):
        self.status_var.set(text)

    def _poll_queue(self):
        try:
            while True:
                item = self.msg_q.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._drain_log(item[1], item[2])
                elif kind == "status":
                    self.set_status(item[1])
                elif kind == "busy_off":
                    self._set_busy(False)
                elif kind == "refresh_plans":
                    self.refresh_plans()
                elif kind == "refresh_net":
                    self.refresh_adapters()
                    self.refresh_ip_info()
                elif kind == "ui":
                    try:
                        item[1]()
                    except tk.TclError:
                        pass
                    except Exception:
                        import traceback
                        traceback.print_exc()
        except queue.Empty:
            pass
        if not self._closing:
            self.after(100, self._poll_queue)

    def run_worker(self, status_text, fn):
        if self.busy:
            messagebox.showinfo("提示", "有任务正在执行，请稍候")
            return
        self._set_busy(True, status_text)

        def w():
            try:
                fn()
            except Exception as e:
                self.log(f"任务异常: {type(e).__name__}: {e}", "error")
            finally:
                self.msg_q.put(("busy_off",))

        threading.Thread(target=w, daemon=True).start()

    def _set_busy(self, busy, text=None):
        self.busy = busy
        if text:
            self.set_status(text)
        else:
            self.set_status("就绪")

    # ---------- 电源页动作 ----------

    def refresh_plans(self):
        plans = list_plans()
        self.plan_tree.delete(*self.plan_tree.get_children())
        active = None
        for guid, name, act in plans:
            state = "★ 活动" if act else ""
            iid = self.plan_tree.insert("", tk.END, values=(name, guid, state))
            if act:
                active = iid
                self.plan_tree.selection_set(iid)
        if active:
            self.plan_tree.focus(active)
        self.log(f"已列出 {len(plans)} 个电源计划", "info")

    def _selected_plan(self):
        sel = self.plan_tree.selection()
        if not sel:
            return None
        vals = self.plan_tree.item(sel[0], "values")
        return vals[1].lower(), vals[0]

    def activate_selected_plan(self):
        sel = self._selected_plan()
        if not sel:
            messagebox.showinfo("提示", "请先选择一个电源计划")
            return
        guid, name = sel

        def w():
            self.log(f"切换电源计划 → {name}", "cmd")
            code, out = activate_plan(guid)
            if code == 0:
                self.log(f"✓ 当前计划：{name}", "success")
            else:
                self.log(f"✗ 切换失败: {out}", "error")
            self.msg_q.put(("refresh_plans",))
            self.msg_q.put(("status",)) if False else None

        self.run_worker(f"正在切换到 {name}…", w)

    def create_new_plan(self):
        name = simpledialog.askstring("新建电源计划", "请输入计划名称：", parent=self)
        if not name:
            return
        name = name.strip()
        if not name:
            return

        def w():
            self.log(f"新建电源计划：{name}（基于平衡计划复制）", "cmd")
            guid, msg = create_plan(name)
            if guid:
                self.log(f"✓ 已创建: {guid}", "success")
                self.msg_q.put(("refresh_plans",))
            else:
                self.log(f"✗ 创建失败: {msg}", "error")

        self.run_worker("正在创建电源计划…", w)

    def delete_selected_plan(self):
        sel = self._selected_plan()
        if not sel:
            messagebox.showinfo("提示", "请先选择要删除的计划")
            return
        guid, name = sel
        cur = active_plan_guid()
        if guid == cur:
            messagebox.showerror("无法删除", "不能删除当前正在使用的电源计划，请先切换到其他计划。")
            return
        if not messagebox.askyesno("确认删除", f"确定删除电源计划「{name}」吗？\n内置计划无法删除。"):
            return

        def w():
            self.log(f"删除电源计划：{name}", "cmd")
            code, out = delete_plan(guid)
            if code == 0:
                self.log("✓ 已删除", "success")
            else:
                self.log(f"✗ 删除失败: {out}", "error")
            self.msg_q.put(("refresh_plans",))

        self.run_worker("正在删除…", w)

    def refresh_timeouts(self):
        disp = query_idle("VIDEOIDLE")
        slp = query_idle("STANDBYIDLE")
        if disp:
            self.var_ac_display.set(str(disp[0] // 60))
            self.var_dc_display.set(str(disp[1] // 60))
        if slp:
            self.var_ac_sleep.set(str(slp[0] // 60))
            self.var_dc_sleep.set(str(slp[1] // 60))
        self.log("已读取当前超时设置", "info")

    def _read_minutes(self, var, label):
        try:
            v = int(var.get())
            if v < 0 or v > 600:
                raise ValueError
            return v
        except ValueError:
            raise ValueError(f"{label} 需要 0~600 的整数，当前为 {var.get()!r}")

    def apply_timeouts(self):
        try:
            ac_d = self._read_minutes(self.var_ac_display, "接通电源·关闭显示器")
            dc_d = self._read_minutes(self.var_dc_display, "使用电池·关闭显示器")
            ac_s = self._read_minutes(self.var_ac_sleep, "接通电源·睡眠")
            dc_s = self._read_minutes(self.var_dc_sleep, "使用电池·睡眠")
        except ValueError as e:
            messagebox.showerror("输入无效", str(e))
            return
        if max(ac_s, dc_s) > 0 and (ac_d > ac_s and ac_s > 0):
            if not messagebox.askyesno("确认",
                "接通电源时显示器关闭时间晚于睡眠，通常显示器应先关闭。仍要应用吗？"):
                return

        def w():
            self.log(f"应用超时：显示 {ac_d}/{dc_d} 分钟，睡眠 "
                     f"{ac_s if ac_s else '从不'}/{dc_s if dc_s else '从不'} 分钟", "cmd")
            ok = set_timeouts(ac_d * 60, dc_d * 60, ac_s * 60, dc_s * 60,
                              log=lambda m, t="info": self.log(m, t))
            self.log("✓ 超时设置已生效" if ok else "✗ 部分设置失败",
                     "success" if ok else "error")

        self.run_worker("正在应用超时设置…", w)

    def apply_power_mode(self, idx):
        label, ac_d, dc_d, ac_s, dc_s, keywords, wifi_ac, wifi_dc = POWER_MODES[idx]
        if not messagebox.askyesno("确认", f"将应用「{label}」并切换到对应计划，是否继续？"):
            return

        def w():
            plans = list_plans()
            target = None
            for guid, name, act in plans:
                low = name.lower()
                if any(k in name or k in low for k in keywords):
                    target = (guid, name)
                    break
            if target:
                self.log(f"切换计划 → {target[1]}", "cmd")
                code, out = activate_plan(target[0])
                if code != 0:
                    self.log(f"切换计划失败（继续应用超时）: {out}", "warning")
            else:
                self.log("未找到对应计划，仅修改当前计划超时参数", "warning")
            ok = set_timeouts(ac_d * 60, dc_d * 60, ac_s * 60, dc_s * 60,
                              log=lambda m, t="info": self.log(m, t))
            # 无线适配器节能等级
            wok, wmsg = set_wifi_power_mode(wifi_ac, wifi_dc)
            if wok:
                self.log(f"  无线适配器节能 → {WIFI_MODE_NAMES[wifi_ac]}(交流)/"
                         f"{WIFI_MODE_NAMES[wifi_dc]}(电池)", "success")
            else:
                self.log(f"  无线节能设置失败: {wmsg}", "warning")
            disp = lambda v: f"{v}分钟" if v else "从不"
            self.log(f"✓ {label}：显示 {disp(ac_d)}/{disp(dc_d)}，"
                     f"睡眠 {disp(ac_s)}/{disp(dc_s)}" +
                     ("" if ok else "（部分超时设置失败）"),
                     "success" if ok else "warning")
            self.msg_q.put(("refresh_plans",))
            self._queue_timeouts()

        self.run_worker(f"正在应用{label}…", w)

    def _queue_timeouts(self):
        self.after(400, self.refresh_timeouts)

    def refresh_battery(self):
        info = battery_info()
        if info is None:
            self.battery_var.set("未检测到电池（台式机或驱动未就绪）")
            return
        status_map = {1: "正在使用电池", 2: "已接通电源", 3: "已充满",
                      4: "充电中（低电量）", 5: "充电中", 6: "充电中"}
        st = status_map.get(info["status"], f"状态码 {info['status']}")
        pct = info["percent"]
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        self.battery_var.set(f"电量 {pct}%  [{bar}]  {st}")

    def do_power_action(self, action):
        names = {"lock": "锁定", "sleep": "睡眠", "hibernate": "休眠",
                 "shutdown": "关机", "restart": "重启"}
        if action in ("shutdown", "restart"):
            if not messagebox.askyesno(
                    "危险操作", f"确定立即{names[action]}吗？\n未保存的工作将丢失！"):
                return
        elif action in ("sleep", "hibernate"):
            if not messagebox.askokcancel("确认", f"电脑将立即进入{names[action]}。"):
                return

        def w():
            self.log(f"执行电源操作：{names[action]}", "cmd")
            code, out = power_action(action)
            if code != 0:
                self.log(f"✗ {names[action]}失败: {out}", "error")

        self.run_worker(f"正在{names[action]}…", w)

    # ---------- 网络页动作 ----------

    def refresh_adapters(self):
        adapters = net_adapters()
        self.adapter_tree.delete(*self.adapter_tree.get_children())
        up_names = []
        for a in adapters:
            status_zh = "已连接" if a["status"] == "Up" else "已断开"
            self.adapter_tree.insert("", tk.END, iid=a["name"],
                                     values=(a["name"], status_zh, a["speed"], a["mac"]))
            if a["status"] == "Up":
                up_names.append(a["name"])
        self.dns_combo["values"] = up_names if up_names else [a["name"] for a in adapters]
        if up_names and self.dns_iface.get() not in up_names:
            # 默认选第一个非 TAP 的物理网卡
            preferred = [n for n in up_names if not n.startswith("以太网")] or up_names
            self.dns_iface.set(preferred[0])
        # 设备级电源管理列出全部网卡
        all_names = [a["name"] for a in adapters]
        self.nic_pm_combo["values"] = all_names
        if all_names and self.nic_pm_iface.get() not in all_names:
            pref = [n for n in all_names if n == self.dns_iface.get()] or all_names
            self.nic_pm_iface.set(pref[0])

    def _on_adapter_select(self, _event=None):
        pass

    def refresh_ip_info(self):
        cfgs = ip_configuration()
        lines = []
        for name, ipv4, prefix, gw, dns in cfgs:
            if not ipv4:
                continue
            lines.append(f"【{name}】")
            lines.append(f"  IP 地址 : {ipv4}/{prefix}")
            lines.append(f"  默认网关: {gw or '(无)'}")
            lines.append(f"  DNS    : {', '.join(dns) if dns else '(无)'}")
        self.ip_info_text.set("\n".join(lines) if lines else "未获取到活动网络配置")

    def query_public_ip(self):
        def w():
            self.log("查询公网 IP…", "cmd")
            text, ip = public_ip()
            if ip:
                self.log(f"✓ {text}", "success")
            else:
                self.log(f"✗ {text}", "error")

        self.run_worker("正在查询公网 IP…", w)

    def do_ping(self):
        raw_target = self.ping_target.get().strip()
        target = raw_target.split()[0] if raw_target else ""
        try:
            count = int(self.ping_count.get())
            if not 1 <= count <= 20:
                raise ValueError
        except ValueError:
            messagebox.showerror("输入无效", "Ping 次数需为 1~20 的整数")
            return
        if target == "__gateway__":
            cfgs = ip_configuration()
            gw = next((c[3] for c in cfgs if c[3]), "")
            if not gw:
                messagebox.showerror("无网关", "当前没有连接任何网络，找不到默认网关")
                return
            target = gw

        def w():
            self.log(f"Ping {target}（{count} 次）", "cmd")
            r = ping_host(target, count)
            if r is None:
                self.log("✓ Ping 执行失败", "error")
                return
            if r["ok"]:
                self.log(f"  丢包 {r['loss']}%，最短 {r['min']}ms，"
                         f"平均 {r['avg']}ms，最长 {r['max']}ms，TTL={r['ttl']}", "success")
                verdict = "网络很好" if r["avg"] is not None and r["avg"] <= 30 and r["loss"] == 0 \
                    else ("网络一般" if (r["avg"] is None or r["avg"] <= 100) and r["loss"] < 10
                          else "网络较差")
                self.ping_result.set(
                    f"丢包率 {r['loss']}%\n延迟 {r['min']}~{r['max']}ms，平均 {r['avg']}ms\n{verdict}")
            else:
                self.log(f"  丢包率 100%，目标不可达", "error")
                self.ping_result.set("丢包率 100%\n目标不可达")

        self.run_worker(f"正在 Ping {target}…", w)

    # ---------- 网卡电源管理 ----------

    def refresh_wifi_mode(self):
        def w():
            mode = wifi_power_mode()
            if mode is None:
                self.log("✗ 读取无线适配器节能模式失败（可能没有无线网卡）", "error")
                return
            ac, dc = mode
            self.ui(lambda: self.var_wifi_ac.set(f"{ac} - {WIFI_MODE_NAMES[ac]}"))
            self.ui(lambda: self.var_wifi_dc.set(f"{dc} - {WIFI_MODE_NAMES[dc]}"))
            self.log(f"无线节能：交流={WIFI_MODE_NAMES[ac]}，电池={WIFI_MODE_NAMES[dc]}", "info")

        self.run_worker("正在读取无线节能设置…", w)

    def apply_wifi_mode(self):
        try:
            ac = int(self.var_wifi_ac.get().split(" - ")[0])
            dc = int(self.var_wifi_dc.get().split(" - ")[0])
        except (ValueError, IndexError):
            messagebox.showinfo("提示", "请先选择交流和电池的节能等级")
            return
        if not 0 <= ac <= 3 or not 0 <= dc <= 3:
            messagebox.showerror("无效", "节能等级必须在 0~3 之间")
            return

        def w():
            self.log(f"设置无线节能 → 交流:{WIFI_MODE_NAMES[ac]} 电池:{WIFI_MODE_NAMES[dc]}", "cmd")
            ok, msg = set_wifi_power_mode(ac, dc)
            if ok:
                self.log("✓ 无线适配器节能模式已生效", "success")
            elif needs_elevation(msg):
                self.log("✗ 需要管理员权限：请用右上角按钮提权重启", "error")
            else:
                self.log(f"✗ 设置失败: {msg}", "error")

        self.run_worker("正在设置无线节能…", w)

    def refresh_nic_pm(self):
        ifname = self.nic_pm_iface.get()
        if not ifname:
            messagebox.showinfo("提示", "请先选择网卡")
            return

        def w():
            self.log(f"读取网卡电源能力：{ifname}", "cmd")
            info = nic_power_management(ifname)
            if info is None:
                self.ui(lambda: self.nic_pm_state.set(
                    f"{ifname}：驱动未向 Windows 暴露设备级电源管理，无法调节\n"
                    "（MediaTek/部分无线网卡常见，可用上方「计划级无线节能」替代）"))
                self.log("该网卡不支持设备级电源管理查询（System Error 31）", "warning")
                return

            def fill():
                self.var_nic_turnoff.set(info["turnoff"])
                self.var_nic_magic.set(info["magic"])
                self.var_nic_pattern.set(info["pattern"])
                self.nic_pm_state.set(
                    f"{ifname}：\n"
                    f"  允许电脑关闭此设备省电 = {info['turnoff']}\n"
                    f"  魔术包唤醒 = {info['magic']}    模式匹配唤醒 = {info['pattern']}")
            self.ui(fill)
            self.log("✓ 已读取设备级电源状态", "success")

        self.run_worker("正在读取网卡电源设置…", w)

    def apply_nic_pm(self):
        ifname = self.nic_pm_iface.get()
        if not ifname:
            messagebox.showinfo("提示", "请先选择网卡")
            return
        turnoff = self.var_nic_turnoff.get()
        magic = self.var_nic_magic.get()
        pattern = self.var_nic_pattern.get()
        if not messagebox.askyesno(
                "确认",
                f"将修改网卡「{ifname}」的设备电源设置：\n"
                f"  允许关闭设备省电：{turnoff}\n"
                f"  魔术包唤醒：{magic}\n  模式匹配唤醒：{pattern}\n\n"
                "需要管理员权限。是否继续？"):
            return

        def w():
            self.log(f"设置网卡电源：{ifname}", "cmd")
            ok, msg = set_nic_power_management(ifname, turnoff, magic, pattern)
            if ok:
                self.log(f"✓ 已应用: {msg}", "success")
                self.ui(self.refresh_nic_pm)
            elif needs_elevation(msg):
                self.log("✗ 需要管理员权限：请用右上角按钮提权重启", "error")
            else:
                self.log(f"✗ 失败: {msg}", "error")

        self.run_worker("正在设置网卡电源…", w)

    def apply_dns(self):
        ifname = self.dns_iface.get()
        if not ifname:
            messagebox.showinfo("提示", "请先选择网络接口")
            return
        label, preset = DNS_PRESETS[self.dns_choice.get()]
        if not messagebox.askyesno("确认", f"将接口「{ifname}」的 DNS 设置为：\n{label}\n是否继续？"):
            return

        def w():
            self.log(f"设置 DNS [{ifname}] → {label}", "cmd")
            code, out = set_dns(ifname, preset)
            if code == 0:
                self.log("✓ DNS 设置成功", "success")
                code2, _ = flush_dns()
                if code2 == 0:
                    self.log("✓ 已顺带刷新 DNS 缓存", "success")
            elif needs_elevation(out):
                self.log("✗ 需要管理员权限：请点击右上角「以管理员身份重启」后重试", "error")
            else:
                self.log(f"✗ DNS 设置失败: {out}", "error")
            self.msg_q.put(("refresh_net",))

        self.run_worker("正在设置 DNS…", w)

    def _reset_tweak_checks(self):
        for key, _l, _c, _a, default, _d in TCP_TWEAKS:
            self.tweak_vars[key].set(default)

    def run_optimizations(self):
        chosen = [(k, l, c, admin) for k, l, c, admin, dflt, _desc in TCP_TWEAKS
                  if self.tweak_vars[k].get()]
        if not chosen:
            messagebox.showinfo("提示", "请至少勾选一项")
            return
        admin_items = [l for k, l, c, admin in chosen if admin]
        tip = ""
        if admin_items and not is_admin():
            tip = ("\n\n以下项目需要管理员权限，当前为标准权限会失败：\n- "
                   + "\n- ".join(admin_items)
                   + "\n\n仍可继续，成功的项目照常生效。")
        if not messagebox.askyesno("确认", f"将依次执行 {len(chosen)} 项优化。{tip}\n\n是否继续？"):
            return

        def w():
            ok_n, fail_n, need_admin_n = 0, 0, 0
            for key, label, cmd, _admin in chosen:
                self.log(f"▶ {label}", "cmd")
                if cmd == ["__DHCP__"]:
                    code, out = run_cmd(["ipconfig", "/release"], timeout=25)
                    self.log(f"  /release: {code}", "info")
                    code, out = run_cmd(["ipconfig", "/renew"], timeout=35)
                else:
                    code, out = run_cmd(cmd)
                if code == 0:
                    ok_n += 1
                    self.log(f"  ✓ 完成", "success")
                elif needs_elevation(out):
                    fail_n += 1
                    need_admin_n += 1
                    self.log(f"  ✗ 需要管理员权限", "error")
                else:
                    fail_n += 1
                    first_line = (out or "").splitlines()[0] if out else "未知错误"
                    self.log(f"  ✗ 失败: {first_line}", "error")
            self.log(f"优化结束：成功 {ok_n} 项，失败 {fail_n} 项"
                     + (f"（其中 {need_admin_n} 项需管理员）" if need_admin_n else ""),
                     "warning" if fail_n else "success")

        self.run_worker("正在执行网络优化…", w)

    def dangerous_reset(self, kind):
        if kind == "winsock":
            title, cmd, hint = ("重置 Winsock 目录",
                                ["netsh", "winsock", "reset"],
                                "Winsock 目录将被重置，VPN/代理类软件可能需要重新安装网络组件。")
        else:
            title, cmd, hint = ("重置 TCP/IP 协议栈",
                                ["netsh", "interface", "ip", "reset"],
                                "TCP/IP 栈将恢复初始状态，所有静态 IP/DNS 设置会丢失。")
        if not messagebox.askyesno("最终确认",
                                   f"确定执行「{title}」吗？\n{hint}\n执行完成后必须重启电脑。"):
            return

        def w():
            self.log(f"执行：{title}", "cmd")
            code, out = run_cmd(cmd, timeout=60)
            if code == 0:
                self.log("✓ 已执行，请尽快重启电脑使其生效", "success")
                if messagebox.askyesno("重启", "是否立即重启电脑？"):
                    run_cmd(["shutdown", "/r", "/t", "0"])
            elif needs_elevation(out):
                self.log("✗ 需要管理员权限：请用右上角按钮以管理员身份重启本程序", "error")
            else:
                self.log(f"✗ 失败: {out}", "error")

        self.run_worker(f"正在{title}…", w)

    def _refresh_tcp_text(self):
        def w():
            out = tcp_global_text()
            self.ui(lambda: self._set_tcp_text(out))

        threading.Thread(target=w, daemon=True).start()

    def _set_tcp_text(self, text):
        self.tcp_text.delete("1.0", tk.END)
        # 只显示有冒号的关键行，去掉空行和分隔线
        lines = [ln.rstrip() for ln in text.splitlines()
                 if ":" in ln or ln.strip().startswith("查询")]
        self.tcp_text.insert("1.0", "\n".join(lines))

    # ---------- 杂项 ----------

    def _render_admin_badge(self):
        if is_admin():
            self.admin_var.set("● 当前权限：管理员（全部功能可用）")
        else:
            self.admin_var.set("○ 当前权限：标准用户（DNS修改/TCP参数/重置等高级操作需要管理员）")

    def relaunch_elevated(self):
        if is_admin():
            messagebox.showinfo("提示", "当前已经是管理员权限。")
            return
        if not messagebox.askyesno("确认", "将以管理员身份重新启动本工具，当前窗口会关闭。是否继续？"):
            return
        try:
            params = " ".join(f'"{a}"' for a in sys.argv)
            rc = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", sys.executable, params, None, 1)
            if rc <= 32:
                messagebox.showerror("失败", f"提权启动失败（错误码 {rc}），可能被 UAC 拒绝。")
                return
            self._closing = True
            self.destroy()
        except Exception as e:
            messagebox.showerror("失败", str(e))

    def _about(self):
        messagebox.showinfo(
            "关于",
            f"{APP_TITLE}\n\n"
            "• 电源：切换/新建/删除计划，显示与睡眠超时，一键省电/平衡/高性能\n"
            "• 网络：接口状态、IP/DNS/公网IP、Ping 测速、DNS 切换、TCP 优化\n\n"
            "所有操作通过 Windows 原生命令（powercfg/netsh/ipconfig）完成，\n"
            "不修改系统文件；危险操作均有二次确认。")

    def _periodic(self):
        if self._closing:
            return
        self.refresh_battery()
        self.after(60_000, self._periodic)

    def _on_close(self):
        self._closing = True
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
