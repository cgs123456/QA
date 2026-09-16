"""网络基线探针（P13）：终结"P4 可达 vs M3 不可达"矛盾。

三段输出（只读探测，不碰真实 key；dummy key 是假值，仅用于证明路径通断——
401/400 = 路径通（对端活着并拒绝了假 key），Timeout/ConnectError = 路径不通）：

  A. 代理环境变量快照（含 Windows 注册表系统代理 + key 配置布尔值，只报有/无不报值）
  B. 直连 TCP（socket.create_connection，不经过任何代理，不发一字节）
  C. 系统代理感知 HTTP（httpx 默认 trust_env；若注册表配了系统代理则加一组显式代理对照）

用法（两种状态各跑一次，见 docs/network-baseline.md）::

    python scripts/net_probe.py                  # 【代理开】系统现状
    python scripts/net_probe.py --direct          # 【代理关】进程级旁路（不改系统注册表）

退出码恒 0（探针只记录，不判定；判定由人看 network-baseline.md 做）。
"""

import argparse
import os
import socket
import sys
import time
import urllib.request

TCP_TARGETS = [
    ("8.8.8.8", 443),
    ("1.1.1.1", 443),
    ("api.openai.com", 443),
    ("api.anthropic.com", 443),
    ("generativelanguage.googleapis.com", 443),
    ("api.groq.com", 443),
]

# (provider, method, url, headers) —— 全是"列目录/探活"端点，dummy key 必被拒。
HTTP_TARGETS = [
    ("openai", "GET", "https://api.openai.com/v1/models",
     {"Authorization": "Bearer sk-probe-DUMMY"}),
    ("groq", "GET", "https://api.groq.com/openai/v1/models",
     {"Authorization": "Bearer sk-probe-DUMMY"}),
    ("claude", "GET", "https://api.anthropic.com/v1/models",
     {"x-api-key": "sk-probe-DUMMY", "anthropic-version": "2023-06-01"}),
    ("gemini", "GET", "https://generativelanguage.googleapis.com/v1beta/models?key=DUMMY",
     {}),
]

KEY_ENVS = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLAUDE_API_KEY",
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY"]


def _reg_proxy():
    """Windows 注册表系统代理（只读）。非 Windows 或读失败返回 (None, None, None)。"""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        enable, _ = winreg.QueryValueEx(k, "ProxyEnable")
        try:
            server, _ = winreg.QueryValueEx(k, "ProxyServer")
        except OSError:
            server = ""
        try:
            override, _ = winreg.QueryValueEx(k, "ProxyOverride")
        except OSError:
            override = ""
        return bool(enable), server, override
    except OSError:
        return None, None, None


def section_a():
    print("== A. 代理环境变量快照 ==")
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                 "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
        val = os.environ.get(name)
        print(f"ENV {name}={'<set:' + str(len(val)) + 'chars>' if val else '<unset>'}")
    enable, server, override = _reg_proxy()
    print(f"REG ProxyEnable={enable} ProxyServer={server!r} ProxyOverride={override!r}")
    try:
        print(f"urllib.getproxies={urllib.request.getproxies()}")
    except Exception as e:  # noqa: BLE001 —— 快照行不能炸探针
        print(f"urllib.getproxies=<error {type(e).__name__}>")
    if server:
        host = server.split(":")[0].split("=")[-1].strip()
        try:
            port = int(server.rsplit(":", 1)[1])
        except ValueError:
            port = 80
        t0 = time.monotonic()
        try:
            s = socket.create_connection((host, port), timeout=3)
            print(f"REG-PROXY-PORT {host}:{port} OPEN {(time.monotonic()-t0)*1000:.0f}ms")
            s.close()
        except OSError as e:
            print(f"REG-PROXY-PORT {host}:{port} CLOSED {type(e).__name__}")
    for k in KEY_ENVS:
        print(f"KEY {k}={'set' if os.environ.get(k, '').strip() else 'unset'}")


def section_b(timeout=5.0):
    print("== B. 直连 TCP（不经代理，不发字节） ==")
    for host, port in TCP_TARGETS:
        t0 = time.monotonic()
        try:
            s = socket.create_connection((host, port), timeout=timeout)
            print(f"TCP {host}:{port} OK {(time.monotonic()-t0)*1000:.0f}ms")
            s.close()
        except OSError as e:
            print(f"TCP {host}:{port} FAIL {type(e).__name__} {(time.monotonic()-t0)*1000:.0f}ms")


def _http_once(client, name, method, url, headers, timeout=10.0):
    t0 = time.monotonic()
    try:
        r = client.request(method, url, headers=headers, timeout=timeout)
        print(f"HTTP[{name}] {r.status_code} {(time.monotonic()-t0)*1000:.0f}ms {url}")
    except Exception as e:  # noqa: BLE001 —— 探针记录异常种类即结论
        print(f"HTTP[{name}] {type(e).__name__} {(time.monotonic()-t0)*1000:.0f}ms {url}")


def section_c(explicit_proxy=None):
    import httpx
    print(f"== C. 代理感知 HTTP（httpx {httpx.__version__}，dummy key） ==")
    print("-- C1 默认 trust_env（进程环境代理） --")
    with httpx.Client(trust_env=True) as c:
        for name, method, url, headers in HTTP_TARGETS:
            _http_once(c, name, method, url, headers)
    if explicit_proxy:
        print(f"-- C2 显式代理 {explicit_proxy}（对照：系统代理是否真能转发） --")
        os.environ["HTTPS_PROXY"] = explicit_proxy
        os.environ["HTTP_PROXY"] = explicit_proxy
        try:
            with httpx.Client(trust_env=True) as c:
                for name, method, url, headers in HTTP_TARGETS:
                    _http_once(c, name + "+via-proxy", method, url, headers)
        finally:
            os.environ.pop("HTTPS_PROXY", None)
            os.environ.pop("HTTP_PROXY", None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--direct", action="store_true",
                    help="进程级旁路：清代理环境变量 + httpx 不读环境（复现 M3 直连口径；不改系统注册表）")
    args = ap.parse_args()
    if args.direct:
        print("## MODE=direct（进程级旁路；系统注册表未动）")
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                     "http_proxy", "https_proxy", "all_proxy"):
            os.environ.pop(name, None)
        os.environ["NO_PROXY"] = "*"
    else:
        print("## MODE=system（系统现状）")
    section_a()
    section_b()
    enable, server, _ = _reg_proxy()
    explicit = None
    if enable and server and not args.direct:
        host = server if "://" in server else f"http://{server}"
        explicit = host
    # --direct 下 httpx 默认 trust_env 此时已无代理可用，等价直连；仍跑 C1 作对照。
    section_c(explicit_proxy=explicit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
