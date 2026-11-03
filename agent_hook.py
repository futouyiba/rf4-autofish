# -*- coding: utf-8 -*-
"""Agent 接管通道：脚本卡住时把现场交出去，等外部（人或 LLM Agent）下指令。

设计目标（玩家 2026-11-03 指定）：
  * 脚本**不写死**异常处理，只负责"发现异常 + 上报现场"
  * 现场 = 截图 + 状态 JSON，落在 `bot/_stuck/`
  * 外部 Agent 看完截图，把指令写进 `bot/_stuck/cmd.json`
  * 脚本轮询到指令就执行，然后回到常规循环

指令文件格式（JSON，执行后脚本会把它改名成 cmd.done.json）：

    {"action": "click",  "x": 960, "y": 810}          # 点客户区坐标
    {"action": "keys",   "keys": ["esc"], "hold": 0.1} # 按一次键
    {"action": "hold",   "keys": ["f5","f6"], "secs": 2.0}
    {"action": "resume"}                              # 什么都不做，继续
    {"action": "quit"}                                # 停下来交给玩家

后续可以把这个文件通道换成一个 LangChain Agent 的 tool 调用，脚本这边不用改。
"""
import json
import os
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
STUCK_DIR = os.path.join(ROOT, "_stuck")
CMD_PATH = os.path.join(STUCK_DIR, "cmd.json")
LATEST_PATH = os.path.join(STUCK_DIR, "latest.json")


def _ensure_dir():
    os.makedirs(STUCK_DIR, exist_ok=True)


def report(kind: str, detail: str, state: dict | None = None) -> dict:
    """把现场落盘：截图 + JSON。返回本次报告的元信息。"""
    _ensure_dir()
    ts = time.strftime("%Y%m%d_%H%M%S")
    shot = os.path.join(STUCK_DIR, f"{ts}_{kind}.png")
    try:
        import rf4s_adapter as A

        img, _ = A.capture_game()
        img.save(shot)
    except Exception as e:                     # 截图失败也要能上报
        shot = None
        print(f"[agent] 截图失败: {e}", flush=True)

    info = {
        "ts": ts,
        "kind": kind,
        "detail": detail,
        "screenshot": shot,
        "state": state or {},
        "hint": "把指令写进 bot/_stuck/cmd.json（见 agent_hook.py 顶部说明）",
    }
    with open(LATEST_PATH, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)
    with open(os.path.join(STUCK_DIR, f"{ts}_{kind}.json"), "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    print("", flush=True)
    print("[agent] ============== 需要 Agent 接管 ==============", flush=True)
    print(f"[agent] 类型: {kind}", flush=True)
    print(f"[agent] 说明: {detail}", flush=True)
    print(f"[agent] 截图: {shot}", flush=True)
    print(f"[agent] 状态: {json.dumps(state or {}, ensure_ascii=False)}", flush=True)
    print(f"[agent] 等待指令: {CMD_PATH}", flush=True)
    print("[agent] =============================================", flush=True)
    return info


def wait_command(timeout: float = 180.0, poll: float = 0.8) -> dict | None:
    """轮询指令文件；超时返回 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(CMD_PATH):
            try:
                with open(CMD_PATH, encoding="utf-8") as f:
                    cmd = json.load(f)
                done = CMD_PATH.replace(".json", f".done-{int(time.time())}.json")
                os.replace(CMD_PATH, done)
                print(f"[agent] 收到指令: {cmd}", flush=True)
                return cmd
            except Exception as e:
                print(f"[agent] 指令文件读不了: {e}", flush=True)
                try:
                    os.replace(CMD_PATH, CMD_PATH + ".bad")
                except Exception:
                    pass
        time.sleep(poll)
    print("[agent] 等待指令超时", flush=True)
    return None


def execute(cmd: dict) -> str:
    """执行指令。返回 'resume' / 'quit' / 其它动作名。"""
    import pydirectinput as pdi

    action = (cmd or {}).get("action", "resume")
    try:
        if action == "click":
            import rf4s_adapter as A

            A._click_client(int(cmd["x"]), int(cmd["y"]))
        elif action == "keys":
            for k in cmd.get("keys", []):
                pdi.press(k)
                time.sleep(0.15)
        elif action == "hold":
            keys = cmd.get("keys", [])
            for k in keys:
                pdi.keyDown(k)
            time.sleep(float(cmd.get("secs", 1.0)))
            for k in keys:
                pdi.keyUp(k)
        elif action == "wait":
            time.sleep(float(cmd.get("secs", 1.0)))
        elif action == "quit":
            return "quit"
    except Exception as e:
        print(f"[agent] 指令执行失败: {e}", flush=True)
    return action if action != "quit" else "quit"


def escalate(kind: str, detail: str, state: dict | None = None,
             timeout: float = 180.0) -> str:
    """上报 + 等指令 + 执行。返回 'quit' 表示要停机。"""
    report(kind, detail, state)
    cmd = wait_command(timeout)
    if cmd is None:
        return "timeout"
    return execute(cmd)
