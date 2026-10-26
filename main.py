#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
烽沙测评播报 —— GitHub Actions / 常驻服务通用版
纯 Python 标准库，无 pip 依赖。

功能：抓取 Steam + 小黑盒评分，计算较上次变化后推送企业微信。
Steam 使用官方 GetAppReviews Web API；小黑盒兼容当前 result/comment_stats 结构。

运行方式：
  python main.py              # 常驻模式：后台每小时跑 + 提供 HTTP /run 手动触发
  python main.py --once       # 单次模式（GitHub Actions 用，跑完即退出）
  python main.py --port 8080  # 指定 HTTP 端口

环境变量（在托管平台后台配置，密钥不要写进代码）：
  WECOM_CORPID  /  WECOM_AGENTID  /  WECOM_SECRET   # 企业微信自建应用凭证
  WECOM_TOUSERS                                     # 必填，逗号分隔 userid；不配置时拒绝推送
  STEAM_APPID                                       # 默认 1733690
  STATE_FILE                                        # 默认 ./state.json（Railway 挂卷后建议设 /data/state.json）
  LOOP_INTERVAL                                     # 默认 3600 秒
  PORT                                              # 默认 8080
"""

import os
import sys
import json
import time
from datetime import datetime, timedelta, timezone
import threading
import urllib.request
import urllib.parse
import http.server

# ======================= 配置 =======================
APPID = int(os.environ.get("STEAM_APPID", "1733690"))
STATE_FILE = os.environ.get("STATE_FILE", "./state.json")
LOOP_INTERVAL = int(os.environ.get("LOOP_INTERVAL", "3600"))
PORT = int(os.environ.get("PORT", "8080"))

STEAM_API_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
HEYBOX_API_URL = "https://api.xiaoheihe.cn/game/web/get_game_detail/"

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


# ======================= Steam 官方评测 API =======================
def parse_steam_payload(payload):
    response = payload.get("response", payload)
    summary = response.get("query_summary", {})
    total = int(summary.get("total_reviews", 0))
    if total <= 0:
        raise ValueError(f"无有效 query_summary: {json.dumps(payload, ensure_ascii=False)[:300]}")
    return {
        "total": total,
        "pos": int(summary["total_positive"]),
        "neg": int(summary["total_negative"]),
        "desc": summary.get("review_score_desc", "") or "",
    }


def fetch_steam():
    """Fetch the first-page all-language summary from Steam's current API."""
    params = {
        "appid": APPID,
        "filter": 0,
        "day_range": 0,
        "languages": ["all"],
        "review_type": 0,
        "purchase_type": 1,
        "num_per_page": 1,
        "display_language": "schinese",
    }
    url = STEAM_API_URL + "?" + urllib.parse.urlencode({
        "input_json": json.dumps(params, separators=(",", ":"))
    })
    try:
        payload = http_get_json(url, headers=STEAM_HEADERS, timeout=20)
        data = parse_steam_payload(payload)
        data["rate"] = round(data["pos"] / data["total"] * 100, 2)
        return {"ok": True, "data": data}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


# ======================= 小黑盒 抓取（兼容新旧结构） =======================
def parse_heybox_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError(f"响应不是对象: {type(payload).__name__}")
    data_block = payload.get("data")
    game = ((data_block.get("game") if isinstance(data_block, dict) else None)
            or payload.get("result") or {})
    if not isinstance(game, dict) or not game:
        raise ValueError(f"缺少 game/result，顶层字段: {list(payload)}")
    stats = game.get("comment_stats") or {}
    if not isinstance(stats, dict):
        stats = {}
    rating = float(game.get("score") or game.get("game_score") or 0)
    count = int(stats.get("score_comment") or stats.get("comment_count") or 0)
    stars = {i: round(float(stats.get(f"star_{i}") or game.get(f"star_{i}") or 0), 2)
             for i in (5, 4, 3, 2, 1)}
    if rating == 0 and count == 0 and not any(stars.values()):
        raise ValueError(f"结构无评分字段，result 字段: {list(game)}")
    return {"ok": True, "rating": rating, "num": count, "stars": stars}


def fetch_heybox():
    try:
        url = f"{HEYBOX_API_URL}?_time={int(time.time())}&appid={APPID}"
        return parse_heybox_payload(http_get_json(url, headers=HEYBOX_HEADERS))
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


# ======================= 企业微信 单聊 =======================
def get_wecom_token(corpid, secret):
    u = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?corpid={corpid}&corpsecret={secret}"
    d = http_get_json(u)
    if d.get("errcode") != 0 or not d.get("access_token"):
        raise RuntimeError(f"企微 token 接口错误: errcode={d.get('errcode')} errmsg={d.get('errmsg')}")
    return d["access_token"]


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
    with urllib.request.urlopen(req, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if result.get("errcode") != 0:
        raise RuntimeError(f"企微发送失败: errcode={result.get('errcode')} errmsg={result.get('errmsg')}")


def push_wecom(content):
    corpid = os.environ.get("WECOM_CORPID")
    agentid = os.environ.get("WECOM_AGENTID")
    secret = os.environ.get("WECOM_SECRET")
    raw = os.environ.get("WECOM_TOUSERS", "")
    tousers = list(dict.fromkeys(u.strip() for u in raw.split(",") if u.strip()))
    if not (corpid and agentid and secret and tousers):
        raise RuntimeError("缺少企业微信配置；须显式设置凭证和 WECOM_TOUSERS，拒绝使用默认收件名单")
    token = get_wecom_token(corpid, secret)
    failures = []
    for user in tousers:
        try:
            send_wecom(token, agentid, user, content)
        except Exception as e:
            failures.append(f"{user}: {e}")
    if failures:
        raise RuntimeError("部分企微消息发送失败: " + "; ".join(failures))


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

    beijing = timezone(timedelta(hours=8))
    now = datetime.now(beijing).strftime("%Y-%m-%d %H:%M")
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
