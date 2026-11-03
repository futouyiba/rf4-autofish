# -*- coding: utf-8 -*-
"""给 Agent/人用的小命令：向正在等指令的脚本下发动作。

用法：
    python agentctl.py click 960 810      # 点客户区坐标
    python agentctl.py keys esc           # 按一次键
    python agentctl.py hold f5 f6 2.0     # 一起按住 2 秒
    python agentctl.py wait 1.5
    python agentctl.py resume             # 不动作，继续跑
    python agentctl.py quit               # 让脚本停手
    python agentctl.py status             # 看最新一次上报
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
STUCK_DIR = os.path.join(ROOT, "_stuck")
CMD_PATH = os.path.join(STUCK_DIR, "cmd.json")
LATEST_PATH = os.path.join(STUCK_DIR, "latest.json")


def send(cmd: dict):
    os.makedirs(STUCK_DIR, exist_ok=True)
    with open(CMD_PATH, "w", encoding="utf-8") as f:
        json.dump(cmd, f, ensure_ascii=False)
    print("已下发:", cmd)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    a = sys.argv[1]
    if a == "status":
        if not os.path.exists(LATEST_PATH):
            print("还没有上报记录")
            return
        with open(LATEST_PATH, encoding="utf-8") as f:
            print(json.dumps(json.load(f), ensure_ascii=False, indent=2))
        print("等待指令中:", os.path.exists(CMD_PATH))
        return
    if a == "click":
        send({"action": "click", "x": int(sys.argv[2]), "y": int(sys.argv[3])})
    elif a == "keys":
        send({"action": "keys", "keys": sys.argv[2:]})
    elif a == "hold":
        secs = float(sys.argv[-1])
        send({"action": "hold", "keys": sys.argv[2:-1], "secs": secs})
    elif a == "wait":
        send({"action": "wait", "secs": float(sys.argv[2])})
    elif a == "resume":
        send({"action": "resume"})
    elif a == "quit":
        send({"action": "quit"})
    else:
        print("未知动作:", a)


if __name__ == "__main__":
    main()
