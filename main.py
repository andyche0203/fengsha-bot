#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
烽沙测评播报 —— 干净托管版 (Railway / Render / Fly / GitHub Actions 通用)
纯 Python 标准库，无 pip 依赖。

功能：每小时抓取 Steam + 小黑盒 评分 → 企业微信自建应用单聊推 8 人。
相对 Cloudflare 旧版改进：
  - Steam 抓取加真实浏览器头 + 指数退避重试 + 3 个一致样本提前停 + 失败写具体原因
  - 出口 IP 更干净（Railway/Render/Fly 正常云 IP，Steam 不拦），根治 Cloudflare 免费层被限流
  - 状态用文件持久化，替代 Cloudflare KV

运行方式：
  python main.py              # 常驻模式：后台每小时跑 + 提供 HTTP /run 手动触发
  python main.py --once       # 单次模式（GitHub Actions 用，跑完即退出）
  python main.py --port 8080  # 指定 HTTP 端口

环境变量（在托管平台后台配置，密钥不要写进代码）：
  WECOM_CORPID  /  WECOM_AGENTID  /  WECOM_SECRET   # 企业微信自建应用凭证
  WECOM_TOUSERS                                     # 逗号分隔 userid，如 CD1971,CD1718,...（缺省用内置 8 人）
  STEAM_APPID                                       # 默认 1733690
  STATE_FILE                                        # 默认 ./state.json（Railway 挂卷后建议设 /data/state.json）
  LOOP_INTERVAL                                     # 默认 3600 秒
  PORT                                              # 默认 8080
"""

import os
import sys
import json
import time
import random
import threading
import urllib.request
import urllib.error
import urllib.parse
import http.server

# ======================= 配置 =======================
APPID = int(os.environ.get("STEAM_APPID", "1733690"))
STATE_FILE = os.environ.get("STATE_FILE", "./state.json")
LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "3600"))
PORT = int(os.environ.get("PORT", "8080"))

DEFAULT_TOUSERS = ["CD1971", "CD1718", "CD1371", "CD2013",
                   "CD1687", "CD2577"]

STEAM_URL = (
    f"https://store.steampowered.com/appreviews/{APPID}"
    f"?json=1&language=all&purchase_type=all&num_per_page=0&review_type=all"
)
HEYBOX_URL = f"https://api.xiaoheihe.cn/game/web/get_game_detail/?_time={int(time.time())}&appid={APPID}"

STEAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": f"https://store.steampowered.com/app/{APPID}",
}
HEYBOX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) AppleWebKit/605.1.15",
    "Referer": "https://xiaoheihe.cn/",
}


# ======================= 工具 =======================
def http_get_json(url, headers=None, timeout=25):
    h = {"User-Agent": STEAM_HEADERS["User-Agent"], "Accept": "application/json, */*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def jitter(ms):
    return ms + random.uniform(0, 0.4)


# ======================= Steam 抓取（修复核心） =======================
def _steam_once():
    """抓一次 Steam appreviews，成功返回 dict，失败抛异常（含原始响应诊断）。"""
    req = urllib.request.Request(STEAM_URL, headers=STEAM_HEADERS)
    with urllib.request.urlopen(req, timeout=25) as r:
        status = r.status
        raw = r.read().decode("utf-8", "replace")
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    if not raw.strip():
        raise RuntimeError("HTTP 200 但响应体为空(疑似数据中心IP被Steam拦截)")
    d = json.loads(raw)
    q = (d or {}).get("query_summary")
    if not q or int(q.get("total_reviews", 0)) <= 0:
        raise RuntimeError("返回数据无效(total_reviews=0或无query_summary)")
    return {
        "total": int(q.get("total_reviews", 0)),
        "pos": int(q.get("total_positive", 0)),
        "neg": int(q.get("total_negative", 0)),
        "desc": q.get("review_score_desc", "") or "",
    }


def fetch_steam():
    """真实浏览器头 + 指数退避重试 + 3 个一致样本提前停 + 众数投票 + 失败诊断。"""
    samples = []
    last_err = "未知"
    for i in range(8):
        try:
            s = _steam_once()
            samples.append(s)
            if len(samples) >= 3:   # 拿到 3 个就停，少打请求、降低被限流概率
                break
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            time.sleep(jitter(3.0 if e.code in (429, 503) else 1.2))
            continue
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
        time.sleep(jitter(0.9 + i * 0.3))
    if not samples:
        return {"ok": False, "err": last_err}

    # 众数：total_reviews 出现最多的样本（排除偶发异常值）
    tally = {}
    best, best_n = samples[0], 0
    for x in samples:
        tally[x["total"]] = tally.get(x["total"], 0) + 1
        if tally[x["total"]] > best_n:
            best_n, best = tally[x["total"]], x
    best["rate"] = round(best["pos"] / best["total"] * 100, 2) if best["total"] else 0
    return {"ok": True, "data": best}


# ======================= 小黑盒 抓取（沿用已验证路径） =======================
def fetch_heybox():
    try:
        d = http_get_json(HEYBOX_URL, headers=HEYBOX_HEADERS)
        g = (d or {}).get("data", {}).get("game", {})
        if not g:
            # 诊断：把响应结构 dump 出来，方便定位是接口变更还是 IP 被限
            top_keys = list(d.keys()) if isinstance(d, dict) else f"type={type(d).__name__}"
            data_block = (d or {}).get("data")
            data_keys = list(data_block.keys()) if isinstance(data_block, dict) else f"type={type(data_block).__name__}"
            raw_preview = json.dumps(d, ensure_ascii=False)[:300]
            return {"ok": False,
                    "err": f"game字段缺失 | top_keys={top_keys} | data_keys={data_keys} | raw={raw_preview}"}
        cs = g.get("comment_stats", {}) or {}
        return {
            "ok": True,
            "rating": float(g.get("score") or 0),
            "num": int(cs.get("score_comment") or 0),
            "stars": {
                5: float(g.get("star_5") or 0),
                4: float(g.get("star_4") or 0),
                3: float(g.get("star_3") or 0),
                2: float(g.get("star_2") or 0),
                1: float(g.get("star_1") or 0),
            },
        }
    except Exception as e:
        return {"ok": False, "err": f"超时/解析失败({e})"}


# ======================= 企业微信 单聊 =======================
def get_wecom_token(corpid, secret):
    u = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={secret}"
    d = http_get_json(u)
    return d.get("access_token")


def send_wecom(token, agentid, touser, content):
    u = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}"
    body = json.dumps({
        "touser": touser,
        "msgtype": "text",
        "agentid": int(agentid),
        "text": {"content": content},
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        u, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"})
    urllib.request.urlopen(req, timeout=15).read()


def push_wecom(content):
    corpid = os.environ.get("WECOM_CORPID")
    agentid = os.environ.get("WECOM_AGENTID")
    secret = os.environ.get("WECOM_SECRET")
    raw = os.environ.get("WECOM_TOUSERS", "")
    tousers = [u.strip() for u in raw.split(",") if u.strip()] or DEFAULT_TOUSERS

    if not (corpid and agentid and secret):
        print("[WARN] 缺少企业微信凭证，仅打印不推送:\n" + content)
        return
    try:
        token = get_wecom_token(corpid, secret)
        for u in tousers:
            try:
                send_wecom(token, agentid, u, content)
            except Exception as e:
                print(f"[ERR] 发给 {u} 失败: {e}")
    except Exception as e:
        print(f"[ERR] 获取企微 token 失败: {e}")


# ======================= 状态（替代 KV） =======================
def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(s):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False)
    except Exception as e:
        print(f"[ERR] 保存状态失败: {e}")


# ======================= 主流程 =======================
def run_once():
    steam = fetch_steam()
    hey = fetch_heybox()
    st = load_state()
    last_steam = st.get("steam", {})
    last_hey = st.get("hey", {})

    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    lines = ["【烽沙测评播报】", f"时间：{now}"]

    if steam["ok"]:
        rate = steam["data"]["rate"]
        diff = ""
        if last_steam.get("rate") is not None:
            d = round(rate - last_steam["rate"], 2)
            diff = f"｜较上次 {d:+.2f}%"
        lines.append(f"Steam｜{steam['data']['desc']}｜好评率{rate}%{diff}｜评论{steam['data']['total']}")
        st["steam"] = steam["data"]
        st["steam_last_err"] = ""
    else:
        lines.append(f"Steam｜抓取失败({steam['err']})")
        st["steam_last_err"] = steam["err"]

    if hey["ok"]:
        dr = f"{hey['rating'] - last_hey['rating']:+.1f}" if last_hey.get("rating") is not None else "0.0"
        dn = hey["num"] - last_hey["num"] if last_hey.get("num") is not None else 0
        lines.append(f"小黑盒｜评分{hey['rating']}｜较上次 评分{dr} 评论{dn:+}｜评论{hey['num']}")
        lines.append(
            f"五星{hey['stars'][5]}% 四星{hey['stars'][4]}% 三星{hey['stars'][3]}% "
            f"二星{hey['stars'][2]}% 一星{hey['stars'][1]}%")
        st["hey"] = {"rating": hey["rating"], "num": hey["num"]}
    else:
        lines.append(f"小黑盒｜{hey['err']}")

    save_state(st)
    msg = "\n".join(lines)
    push_wecom(msg)
    return msg


def scheduler():
    while True:
        try:
            print(run_once())
        except Exception as e:
            print(f"[ERR] 运行异常: {e}")
        time.sleep(LOOP_INTERVAL)


# ======================= HTTP（手动 / 外部 cron 触发） =======================
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            # 健康检查只回 ok，绝不触发抓取/推送（否则 Railway 频繁探活会刷屏）
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/run"):
            try:
                msg = run_once()
            except Exception as e:
                msg = f"运行出错: {e}"
            body = msg.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *a):
        pass


def main():
    if "--once" in sys.argv:
        print(run_once())
        return
    port = PORT
    for a in sys.argv:
        if a.startswith("--port"):
            port = int(a.split("=")[-1])
    t = threading.Thread(target=scheduler, daemon=True)
    t.start()
    print(f"[INFO] 常驻模式已启动，每 {LOOP_INTERVAL}s 跑一次；HTTP 监听 0.0.0.0:{port}（/run 手动触发）")
    http.server.HTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
