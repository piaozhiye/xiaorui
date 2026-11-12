#!/usr/bin/env python3
"""HA 无头引导 + 长期令牌(xiaorui 部署链专用)。

板上首次起 HA 后跑一次:
  1. onboarding REST 创建用户(已引导过则跳过)
  2. auth_code -> access_token
  3. websocket 调 auth/long_lived_access_token 拿 3650 天 LLAT
  4. 写 /data/xiaorui-server/ha_token.txt (600), 供网关 haIntent 使用

用法: ha-onboard.py --password-stdin   (从标准输入读取密码，无默认密码)
幂等: 已 onboard 时直接走 auth 流程(password 必须与当初一致)。
"""
import argparse
import asyncio
import json
import os
import sys
import tempfile
import urllib.parse
import urllib.request

import aiohttp  # HA 自带依赖, 不额外装包

BASE = "http://127.0.0.1:8123"
CLIENT_ID = BASE + "/"
TOKEN_FILE = "/data/xiaorui-server/ha_token.txt"


def http(method, url, token=None, data=None, form=None):
    headers = {}
    body = None
    if token:
        headers["Authorization"] = "Bearer " + token
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


async def get_llat(access_token):
    """websocket 认证后创建长期令牌。"""
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(BASE + "/api/websocket") as ws:
            msg = await ws.receive_json()          # auth_required
            await ws.send_json({"type": "auth",
                                "access_token": access_token})
            msg = await ws.receive_json()
            if msg["type"] != "auth_ok":
                raise RuntimeError(f"ws 认证失败: {msg}")
            await ws.send_json({"id": 1,
                                "type": "auth/long_lived_access_token",
                                "client_name": "xiaorui-ha",
                                "lifespan": 3650})
            msg = await ws.receive_json()
            if not msg.get("success"):
                raise RuntimeError(f"LLAT 创建失败: {msg}")
            return msg["result"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--password-stdin", action="store_true", required=True)
    ap.parse_args()
    password = sys.stdin.read()
    if not password:
        ap.error("标准输入中的密码不能为空")

    # 1) onboarding(仅首次)
    code = None
    st, cur = http("GET", BASE + "/api/onboarding")
    if st != 200:
        sys.exit(f"HA 不在 8123? {st} {cur}")
    # 返回是步骤列表(如 [{"step":"user","done":false}]), 全部 done 才算引导完成
    done = isinstance(cur, list) and all(s.get("done") for s in cur)
    if not done:
        st, res = http("POST", BASE + "/api/onboarding/users", data={
            "client_id": CLIENT_ID,
            "username": "xiaorui", "password": password,
            "name": "xiaorui",
            "language": "zh-cn"})
        if st != 200 or "auth_code" not in res:
            sys.exit(f"onboarding 失败: {st} {res}")
        code = res["auth_code"]
        print("onboarding: 用户已创建")
    else:
        print("onboarding: 已完成, 走登录流程")

    # 2) access token: 有 auth_code 用 code, 否则用密码走 login_flow
    if code:
        st, tok = http("POST", BASE + "/auth/token", form={
            "grant_type": "authorization_code", "code": code,
            "client_id": CLIENT_ID})
    else:
        st, flow = http("POST", BASE + "/auth/login_flow",
                        data={"client_id": CLIENT_ID,
                              "handler": ["homeassistant", None],
                              "redirect_uri": CLIENT_ID})
        if st != 200:
            sys.exit(f"login_flow 失败: {st} {flow}")
        st, step = http("POST", BASE + "/auth/login_flow/" + flow["flow_id"],
                        data={"client_id": CLIENT_ID,
                              "username": "xiaorui",
                              "password": password})
        if st != 200 or step.get("type") != "create_entry":
            sys.exit(f"密码错误或登录失败: {st} {step}")
        st, tok = http("POST", BASE + "/auth/token", form={
            "grant_type": "authorization_code",
            "code": step["result"], "client_id": CLIENT_ID})
    if st != 200 or "access_token" not in tok:
        sys.exit(f"token 交换失败: {st} {tok}")
    print("access token: OK")

    # 3) LLAT
    llat = asyncio.run(get_llat(tok["access_token"]))
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    descriptor, token_tmp = tempfile.mkstemp(dir=os.path.dirname(TOKEN_FILE), prefix=".ha-token-")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
            token_file.write(llat)
        os.replace(token_tmp, TOKEN_FILE)
    finally:
        if os.path.exists(token_tmp):
            os.unlink(token_tmp)
    print(f"LLAT 已写入 {TOKEN_FILE} (3650 天)")

    # 4) 自检: 用 LLAT 查 states
    st, states = http("GET", BASE + "/api/states", token=llat)
    lights = [s["entity_id"] for s in states
              if s["entity_id"].startswith("light.")]
    print(f"自检: {st}, 实体 {len(states)} 个, 灯: {lights}")


if __name__ == "__main__":
    main()
