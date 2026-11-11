#!/usr/bin/env python3
"""Declarative configurator for the 3x-ui + Tailscale container.

Modes:
  --pre        apply panel / subscription settings to the 3x-ui DB (before panel start)
  --tailscale  start tailscaled and bring the tailnet up (userspace or kernel)
  --post       wait for the panel, login and upsert inbounds / Xray template

Everything is driven by /config/config.yaml.
"""
import base64
import http.cookiejar
import json
import os
import re
import secrets
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("py3-yaml is required")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.yaml")
DB_DIR = os.environ.get("XUI_DB_FOLDER", "/etc/x-ui")
DB_PATH = os.path.join(DB_DIR, "x-ui.db")
STATE_PATH = os.path.join(DB_DIR, "node-state.json")
XUI_BIN = "/app/x-ui"
XRAY_BIN = "/app/bin/xray-linux-amd64"


def log(*args):
    print("[configure]", *args, flush=True)


def cfg_get(cfg, path, default=None):
    cur = cfg
    for key in path.split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def load_cfg():
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return cfg


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def save_state(state):
    os.makedirs(DB_DIR, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def sqlite_upsert(key, value):
    con = sqlite3.connect(DB_PATH)
    try:
        cur = con.execute("update settings set value=? where key=?", (str(value), key))
        if cur.rowcount == 0:
            con.execute("insert into settings(key,value) values(?,?)", (key, str(value)))
        con.commit()
    finally:
        con.close()


def run(cmd, **kwargs):
    log("run:", " ".join(cmd))
    return subprocess.run(cmd, check=kwargs.pop("check", True), **kwargs)


# --------------------------------------------------------------------------- #
# pre                                                                          #
# --------------------------------------------------------------------------- #
def apply_pre(cfg):
    panel = cfg.get("panel", {}) or {}
    port = int(panel.get("port", 2053))
    base_path = str(panel.get("web_base_path", "/") or "/")
    args = [
        XUI_BIN, "setting",
        "-username", str(panel.get("username", "admin")),
        "-password", str(panel.get("password", "admin")),
        "-port", str(port),
        "-webBasePath", base_path,
    ]
    if panel.get("listen"):
        args += ["-listenIP", str(panel["listen"])]
    if panel.get("cert_file") and panel.get("key_file"):
        args += ["-webCert", str(panel["cert_file"]), "-webCertKey", str(panel["key_file"])]
    run(args)

    sub = cfg.get("subscription", {}) or {}
    if sub.get("enabled", True):
        path = str(sub.get("path") or ("/" + secrets.token_hex(8) + "/"))
        if not path.startswith("/"):
            path = "/" + path
        if not path.endswith("/"):
            path += "/"
        sqlite_upsert("subDomain", sub.get("domain", ""))
        sqlite_upsert("subPort", int(sub.get("port", 2096)))
        sqlite_upsert("subPath", path)
        if sub.get("cert_file") and sub.get("key_file"):
            sqlite_upsert("subCertFile", sub["cert_file"])
            sqlite_upsert("subKeyFile", sub["key_file"])
        log("subscription:", sub.get("domain"), sub.get("port"), path)
    log("panel configured on port", port, "base path", base_path)


# --------------------------------------------------------------------------- #
# tailscale                                                                    #
# --------------------------------------------------------------------------- #
def apply_tailscale(cfg):
    ts = cfg.get("tailscale", {}) or {}
    if not ts.get("enabled", True):
        log("tailscale disabled")
        return
    state_dir = str(ts.get("state_dir", "/var/lib/tailscale"))
    os.makedirs(state_dir, exist_ok=True)
    sock = os.path.join(state_dir, "tailscaled.sock")
    state = os.path.join(state_dir, "tailscaled.state")
    mode = str(ts.get("mode", "userspace")).lower()

    cmd = ["tailscaled", "--state=" + state, "--socket=" + sock]
    if mode == "userspace":
        socks_port = int(ts.get("socks_port", 1055))
        cmd += ["--tun=userspace-networking", "--socks5-server=127.0.0.1:%d" % socks_port]
    log("starting tailscaled (%s)" % mode)
    subprocess.Popen(cmd, stdout=sys.stdout, stderr=sys.stderr)

    for _ in range(120):
        if os.path.exists(sock):
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("tailscaled socket did not appear")

    auth = str(ts.get("auth_key") or os.environ.get("TS_AUTHKEY", "") or "")
    up = ["tailscale", "--socket=" + sock, "up",
          "--hostname=" + str(ts.get("hostname", "xray-node"))]
    if auth:
        up.append("--auth-key=" + auth)
    if ts.get("exit_node"):
        up.append("--exit-node=" + str(ts["exit_node"]))
    if ts.get("exit_node_allow_lan_access"):
        up.append("--exit-node-allow-lan-access")
    if ts.get("accept_routes"):
        up.append("--accept-routes")
    if ts.get("advertise_routes"):
        up.append("--advertise-routes=" + ",".join(str(x) for x in ts["advertise_routes"]))
    if ts.get("advertise_exit_node"):
        up.append("--advertise-exit-node")
    run(up, timeout=180)

    for _ in range(60):
        out = subprocess.run(["tailscale", "--socket=" + sock, "status", "--json"],
                             capture_output=True, text=True)
        try:
            if json.loads(out.stdout).get("BackendState") == "Running":
                break
        except Exception:
            pass
        time.sleep(1)
    log("tailscale is up")


# --------------------------------------------------------------------------- #
# panel API                                                                    #
# --------------------------------------------------------------------------- #
class PanelAPI:
    def __init__(self, port, base_path="/"):
        base = "http://127.0.0.1:%d" % port
        bp = (base_path or "/").strip("/")
        if bp:
            base += "/" + bp
        self.base = base
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj),
            urllib.request.HTTPSHandler(context=ssl._create_unverified_context()),
        )

    def _open(self, req):
        return self.opener.open(req, timeout=30)

    def csrf(self):
        with self._open(self.base + "/csrf-token") as resp:
            return json.load(resp)["obj"]

    def request(self, path, payload=None, form=None, method=None):
        headers = {
            "X-CSRF-Token": self.csrf(),
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
        }
        body = None
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        elif form is not None:
            body = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(
            self.base + path, data=body, headers=headers,
            method=method or ("POST" if body is not None else "GET"),
        )
        with self._open(req) as resp:
            text = resp.read().decode()
        return json.loads(text) if text else {}

    def login(self, username, password):
        token = self.csrf()
        req = urllib.request.Request(
            self.base + "/login",
            data=json.dumps({"username": username, "password": password}).encode(),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": token,
                "X-Requested-With": "XMLHttpRequest",
            },
            method="POST",
        )
        with self._open(req) as resp:
            data = json.loads(resp.read().decode())
        if not data.get("success"):
            raise RuntimeError("panel login failed: %s" % data.get("msg"))


def wait_login(api, username, password, timeout=180):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            api.login(username, password)
            log("panel login ok")
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(2)
    raise RuntimeError("could not log into panel: %s" % last)


# --------------------------------------------------------------------------- #
# inbounds / xray                                                              #
# --------------------------------------------------------------------------- #
def ensure_secrets(cfg, state):
    reality = cfg.get("reality", {}) or {}
    private_key = str(reality.get("private_key") or state.get("reality_private") or "")
    public_key = str(reality.get("public_key") or state.get("reality_public") or "")
    if not private_key:
        out = subprocess.run([XRAY_BIN, "x25519"], capture_output=True, text=True, check=True).stdout
        m = re.search(r"PrivateKey:\s*(\S+)", out)
        p = re.search(r"(?:Password \(PublicKey\)|PublicKey):\s*(\S+)", out)
        if not m:
            raise RuntimeError("could not parse x25519 output: %s" % out)
        private_key = m.group(1)
        public_key = p.group(1) if p else ""
    if not public_key:
        # derive from private key
        out = subprocess.run([XRAY_BIN, "x25519", "-i", private_key],
                             capture_output=True, text=True, check=True).stdout
        p = re.search(r"(?:Password \(PublicKey\)|PublicKey):\s*(\S+)", out)
        public_key = p.group(1) if p else ""
    short_ids = reality.get("short_ids") or state.get("short_ids") or []
    if not short_ids:
        short_ids = [secrets.token_hex(8)]
    state.update({"reality_private": private_key, "reality_public": public_key,
                  "short_ids": short_ids})
    return private_key, public_key, short_ids


def build_clients(cfg, state):
    clients = []
    vless = cfg.get("vless", {}) or {}
    state.setdefault("clients", {})
    state.setdefault("subids", {})
    for item in (vless.get("clients") or [{"name": "client1"}]):
        name = str(item.get("name") or "client")
        uid = str(item.get("uuid") or state["clients"].get(name) or uuid.uuid4())
        state["clients"][name] = uid
        sub_id = str(item.get("sub_id") or state["subids"].get(name) or secrets.token_hex(8))
        state["subids"][name] = sub_id
        client = {
            "id": uid, "flow": str(item.get("flow", "")), "email": name,
            "limitIp": 0, "totalGB": 0, "expiryTime": 0, "enable": True,
            "tgId": "", "subId": sub_id, "comment": "", "reset": 0,
        }
        clients.append(client)
    return clients


def merge_existing_clients(existing_inbound, desired):
    """Preserve panel generated subId/created_at for clients with the same email."""
    if not existing_inbound:
        return desired
    old = {}
    settings = existing_inbound.get("settings")
    if isinstance(settings, str):
        try:
            settings = json.loads(settings)
        except Exception:
            settings = {}
    for client in (settings or {}).get("clients", []) or []:
        if client.get("email"):
            old[client["email"]] = client
    for client in desired:
        prev = old.get(client["email"])
        if prev:
            # keep the panel-generated subscription id if it is already set
            if prev.get("subId"):
                client["subId"] = prev["subId"]
            for key in ("created_at", "updated_at", "resetDay", "resetWeekday",
                        "resetMax", "trafficReset", "trafficResetDay"):
                if key in prev:
                    client[key] = prev[key]
    return desired


def build_vless_payload(cfg, secrets, existing):
    vless = cfg.get("vless", {}) or {}
    reality = cfg.get("reality", {}) or {}
    port = int(vless.get("port", 10890))
    private_key, public_key, short_ids = secrets
    clients = merge_existing_clients(existing, build_clients(cfg, STATE_CACHE))
    stream = {
        "network": "tcp",
        "security": "reality",
        "externalProxy": [],
        "realitySettings": {
            "show": False,
            "xver": 0,
            "target": str(reality.get("target", "www.yandex.ru:443")),
            "serverNames": list(reality.get("server_names") or ["www.yandex.ru"]),
            "privateKey": private_key,
            "minClientVer": "", "maxClientVer": "", "maxTimediff": 0,
            "shortIds": list(short_ids),
            "settings": {
                "publicKey": public_key,
                "fingerprint": str(reality.get("fingerprint", "chrome")),
                "serverName": "",
                "spiderX": str(reality.get("spider_x", "/")),
            },
        },
        "tcpSettings": {"acceptProxyProtocol": False, "header": {"type": "none"}},
    }
    settings = {
        "clients": clients,
        "decryption": "none",
        "fallbacks": [],
    }
    sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic", "fakedns"],
                "metadataOnly": False, "routeOnly": False}
    return {
        "up": 0, "down": 0, "total": 0,
        "remark": str(vless.get("remark", "vless-" + str(cfg.get("node", {}).get("name", "node")))),
        "enable": True, "expiryTime": 0,
        "listen": str(vless.get("listen", "")),
        "port": port, "protocol": "vless",
        "settings": json.dumps(settings, ensure_ascii=False),
        "streamSettings": json.dumps(stream, ensure_ascii=False),
        "sniffing": json.dumps(sniffing, ensure_ascii=False),
        "tag": "in-%d-vless" % port,
        "shareAddrStrategy": "custom",
        "shareAddr": str(vless.get("share_domain", "")),
    }


def build_mixed_payload(cfg):
    socks = cfg.get("socks", {}) or {}
    port = int(socks.get("port", 10888))
    if str(socks.get("auth", "noauth")).lower() == "password":
        settings = {
            "auth": "password",
            "accounts": [{"user": str(socks.get("username", "user")),
                          "pass": str(socks.get("password", "pass"))}],
            "udp": True,
        }
    else:
        settings = {"auth": "noauth", "udp": True}
    stream = {"network": "tcp", "security": "none"}
    sniffing = {"enabled": True, "destOverride": ["http", "tls", "quic", "fakedns"],
                "metadataOnly": False, "routeOnly": False}
    return {
        "up": 0, "down": 0, "total": 0,
        "remark": "mixed-%d" % port, "enable": True, "expiryTime": 0,
        "listen": str(socks.get("listen", "0.0.0.0")),
        "port": port, "protocol": "mixed",
        "settings": json.dumps(settings, ensure_ascii=False),
        "streamSettings": json.dumps(stream, ensure_ascii=False),
        "sniffing": json.dumps(sniffing, ensure_ascii=False),
        "tag": "in-%d-mixed" % port,
    }


def upsert_inbound(api, payload):
    listing = api.request("/panel/api/inbounds/list", method="GET")
    inbounds = listing.get("obj") or []
    existing = None
    for item in inbounds:
        if item.get("tag") == payload["tag"] or item.get("port") == payload["port"]:
            existing = item
            break
    if existing:
        payload = dict(payload)
        payload["id"] = existing["id"]
        payload["up"] = existing.get("up", 0)
        payload["down"] = existing.get("down", 0)
        payload["total"] = existing.get("total", 0)
        # preserve panel generated client subIds
        try:
            payload["settings"] = json.dumps(
                {"clients": merge_existing_clients(existing, json.loads(payload["settings"])["clients"]),
                 "decryption": "none", "fallbacks": []}, ensure_ascii=False)
        except Exception:
            pass
        resp = api.request("/panel/api/inbounds/update/%s" % existing["id"], payload=payload)
        log("updated inbound", payload["tag"])
        return resp, existing
    resp = api.request("/panel/api/inbounds/add", payload=payload)
    log("added inbound", payload["tag"])
    return resp, resp.get("obj")


def build_xray_config(cfg, api):
    resp = api.request("/panel/api/xray/", method="POST")
    obj = resp.get("obj")
    if isinstance(obj, str):
        obj = json.loads(obj)
    conf = obj.get("xraySetting", obj)
    if isinstance(conf, str):
        conf = json.loads(conf)

    outbound = str(cfg_get(cfg, "xray.outbound", "tailscale-socks"))
    socks_host = str(cfg_get(cfg, "xray.tailscale_socks_host", "127.0.0.1"))
    socks_port = int(cfg_get(cfg, "xray.tailscale_socks_port", 1055))
    vps_addr = str(cfg_get(cfg, "xray.vps_socks_address", "100.116.29.54"))
    vps_port = int(cfg_get(cfg, "xray.vps_socks_port", 1080))

    outbounds = [o for o in conf.get("outbounds", [])
                 if o.get("tag") not in ("tailscale-socks", "vps-socks")]
    if not any(o.get("tag") == "direct" for o in outbounds):
        outbounds.append({"tag": "direct", "protocol": "freedom"})
    if not any(o.get("tag") == "blocked" for o in outbounds):
        outbounds.append({"tag": "blocked", "protocol": "blackhole"})

    if outbound in ("tailscale-socks", "vps-socks"):
        outbounds.append({
            "tag": "tailscale-socks", "protocol": "socks",
            "settings": {"servers": [{"address": socks_host, "port": socks_port}]},
        })
        proxy_tag = "tailscale-socks"
        if outbound == "vps-socks":
            outbounds.append({
                "tag": "vps-socks", "protocol": "socks",
                "settings": {"servers": [{"address": vps_addr, "port": vps_port}]},
                "streamSettings": {"sockopt": {"dialerProxy": "tailscale-socks"}},
            })
            proxy_tag = "vps-socks"
    elif outbound == "direct":
        proxy_tag = "direct"
    else:
        raise RuntimeError("unknown xray.outbound: %s" % outbound)
    conf["outbounds"] = outbounds

    routing = conf.setdefault("routing", {})
    rules = routing.get("rules", [])
    # drop previous generated direct/proxy rules, keep system ones
    rules = [r for r in rules if not (r.get("outboundTag") in ("direct", "tailscale-socks", "vps-socks"))]
    # api + private + bittorrent rules are kept; re-add ours before catch-all
    direct_domains = list(cfg_get(cfg, "routing.direct_domains", []) or [])
    direct_geosite = list(cfg_get(cfg, "routing.direct_geosite", []) or [])
    direct_geoip = list(cfg_get(cfg, "routing.direct_geoip", []) or [])
    if direct_geosite:
        rules.append({"type": "field", "domain": direct_geosite, "outboundTag": "direct"})
    if direct_domains:
        rules.append({"type": "field", "domain": direct_domains, "outboundTag": "direct"})
    if direct_geoip:
        rules.append({"type": "field", "ip": direct_geoip, "outboundTag": "direct"})
        routing["domainStrategy"] = "IPIfNonMatch"
    elif not routing.get("domainStrategy"):
        routing["domainStrategy"] = "AsIs"
    rules.append({"type": "field", "network": "tcp,udp", "outboundTag": proxy_tag})
    routing["rules"] = rules
    conf["routing"] = routing

    form = {
        "xraySetting": json.dumps(conf, ensure_ascii=False),
        "outboundTestUrl": str(cfg_get(cfg, "xray.test_url", "https://www.gstatic.com/generate_204")),
    }
    resp = api.request("/panel/api/xray/update", form=form)
    log("xray template updated, proxy =", proxy_tag)
    return resp


def apply_mss_gso(cfg):
    vless = cfg.get("vless", {}) or {}
    port = int(vless.get("port", 10890))
    mss = cfg.get("mss", {}) or {}
    if mss.get("enabled", True):
        value = str(mss.get("value", 1360))
        for spec in (["-t", "mangle", "PREROUTING", "-p", "tcp", "--dport", str(port), "--tcp-flags", "SYN,RST", "SYN", "-j", "TCPMSS", "--set-mss", value],
                     ["-t", "mangle", "OUTPUT", "-p", "tcp", "--sport", str(port), "--tcp-flags", "SYN,RST", "SYN", "-j", "TCPMSS", "--set-mss", value]):
            try:
                subprocess.run(["iptables"] + spec, check=True)
                log("mss clamp applied", value)
            except Exception as exc:  # noqa: BLE001
                log("mss clamp skipped:", exc)
    gso = cfg.get("gso", {}) or {}
    if gso.get("off", False):
        try:
            subprocess.run(["ethtool", "-K", "eth0", "gso", "off", "tso", "off", "gro", "off"], check=True)
            log("gso/tso/gro disabled")
        except Exception as exc:  # noqa: BLE001
            log("gso off skipped:", exc)


def sync_client_subids(state):
    """3x-ui keeps subId in the clients table; the panel API does not accept it
    in the inbound settings, so write it directly (WAL-safe, read per request)."""
    con = sqlite3.connect(DB_PATH)
    try:
        for email, sub_id in (state.get("subids") or {}).items():
            con.execute("update clients set sub_id=? where email=?", (sub_id, email))
        con.commit()
    finally:
        con.close()


def print_links(cfg, state, vless_inbound):
    vless = cfg.get("vless", {}) or {}
    reality = cfg.get("reality", {}) or {}
    domain = str(vless.get("share_domain", ""))
    ext_port = int(vless.get("external_port", vless.get("port", 10890)))
    server_name = str((reality.get("server_names") or ["www.yandex.ru"])[0])
    pub = state.get("reality_public", "")
    short_id = (state.get("short_ids") or [""])[0]
    spx = urllib.parse.quote(str(reality.get("spider_x", "/")), safe="")
    clients = state.get("clients", {})
    log("========== CLIENT LINKS ==========")
    for client in (vless_inbound or {}).get("settings", {}).get("clients", []) if isinstance((vless_inbound or {}).get("settings"), dict) else []:
        email = client.get("email", "")
        uid = client.get("id") or clients.get(email, "")
        name = urllib.parse.quote(email)
        link = ("vless://%s@%s:%d?type=tcp&security=reality&encryption=none"
                "&pbk=%s&fp=chrome&sni=%s&sid=%s&spx=%s#%s") % (
            uid, domain, ext_port, pub, server_name, short_id, spx, name)
        log(link)
        sub_id = client.get("subId") or (state.get("subids") or {}).get(email, "")
        sub = cfg.get("subscription", {}) or {}
        if sub.get("enabled", True) and sub_id:
            scheme = "https" if (sub.get("cert_file") and sub.get("key_file")) else "http"
            sub_url = "%s://%s:%d%s%s" % (
                scheme, sub.get("domain", domain), int(sub.get("external_port", sub.get("port", 2096))),
                str(sub.get("path", "/sub/")), sub_id)
            log("SUB:", sub_url)
    log("==================================")


STATE_CACHE = {}


# --------------------------------------------------------------------------- #
# post                                                                         #
# --------------------------------------------------------------------------- #
def apply_post(cfg):
    global STATE_CACHE
    panel = cfg.get("panel", {}) or {}
    port = int(panel.get("port", 2053))
    base_path = str(panel.get("web_base_path", "/") or "/")
    api = PanelAPI(port, base_path)
    wait_login(api, str(panel.get("username", "admin")), str(panel.get("password", "admin")))

    state = load_state()
    STATE_CACHE = state
    secrets = ensure_secrets(cfg, state)
    save_state(state)

    vless_payload = build_vless_payload(cfg, secrets, None)
    # upsert does merge with existing; pass existing from list via upsert
    vless_resp, vless_inbound = upsert_inbound(api, vless_payload)
    if not vless_inbound:
        vless_inbound = vless_resp.get("obj")

    socks = cfg.get("socks", {}) or {}
    if socks.get("enabled", True):
        upsert_inbound(api, build_mixed_payload(cfg))

    sync_client_subids(state)
    build_xray_config(cfg, api)
    apply_mss_gso(cfg)

    # refresh inbound after update to print subIds
    listing = api.request("/panel/api/inbounds/list", method="GET")
    for item in listing.get("obj") or []:
        if item.get("tag") == vless_payload["tag"]:
            vless_inbound = item
            break
    print_links(cfg, state, vless_inbound)
    save_state(state)


# --------------------------------------------------------------------------- #
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--post"
    cfg = load_cfg()
    if mode == "--pre":
        apply_pre(cfg)
    elif mode == "--tailscale":
        apply_tailscale(cfg)
    elif mode == "--post":
        apply_post(cfg)
    else:
        sys.exit("unknown mode: %s" % mode)


if __name__ == "__main__":
    main()
