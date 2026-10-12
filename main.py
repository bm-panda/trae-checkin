#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trae 每日签到 · 不忙脚本盒子入口适配器
────────────────────────────────────────────────────────────
盒子把参数 JSON 路径作为 argv[1] 传入。本适配器负责：
  1. 解析盒子的 environment / data / params 三段契约
  2. 自动从本机已登录的 Trae 客户端提取凭证与设备密钥（提取 + 签到一次完成）
  3. 按 action 分派到 trae_checkin.py（签到）/ trae_credit_monitor.py（积分查询）
  4. 执行结果在命令行打印，并在定时任务时写入回传信封 output_json

上游脚本保持原样，仅各加了一行 return 汇总，方便长期跟随更新。
────────────────────────────────────────────────────────────
"""

import importlib
import json
import os
import sys
import time

# 签到结果里的「硬失败」状态（不算签到成功、也不算可重试）
HARD_FAIL = {"失败", "异常", "鉴权失败", "未开放", "无token", "token失效"}


def apply_params(params):
    """把盒子 params 注入环境变量。当前仅「登录文件路径」作为手动兜底。"""
    storage_path = params.get("storage_path")
    if storage_path and str(storage_path).strip():
        os.environ["TRAE_STORAGE_PATH"] = str(storage_path).strip()


def autofill_accounts_from_local():
    """从本机已登录的 Trae 客户端提取凭据与设备密钥，注入环境变量。

    让「提取凭证 + 签到」一次完成：签名前先解密本机 storage.json，
    取出 refreshToken / accessToken / uid 与设备证明材料。
    返回 (ok, msg)，失败时 msg 说明原因。
    """
    if os.environ.get("TRAE_ACCOUNTS"):
        return True, ""
    try:
        module = importlib.import_module("trae_checkin")
    except Exception as exc:  # noqa: BLE001
        return False, "加载签到脚本失败: %s" % exc
    try:
        storage_path = os.environ.get("TRAE_STORAGE_PATH") or module.find_storage_json()
    except Exception as exc:  # noqa: BLE001
        return False, "探测本机登录文件失败: %s" % exc
    if not storage_path:
        return False, "未找到本机 Trae 客户端登录文件，请先登录 Trae 客户端，或在界面填写「登录文件路径」"
    try:
        creds, real_device_id = module.load_from_storage(storage_path)
    except Exception as exc:  # noqa: BLE001
        return False, "解密登录文件失败（%s）: %s" % (storage_path, exc)
    account = module.normalize(creds, "local-storage", "", real_device_id)
    account["realDeviceId"] = real_device_id
    os.environ["TRAE_ACCOUNTS"] = json.dumps([account], ensure_ascii=False)
    # 顺带注入本机设备证明材料，提升 Token 续期成功率
    try:
        keys = module.local_device_keys() or {}
    except Exception:
        keys = {}
    for env_name, key_name in (
        ("TRAE_DEVICE_KEY_PEM", "privateKeyPem"),
        ("TRAE_DEVICE_PUB_PEM", "publicKeyPem"),
        ("TRAE_DEVICE_ID", "deviceId"),
        ("TRAE_MACHINE_ID", "machineId"),
    ):
        value = keys.get(key_name)
        if value and not os.environ.get(env_name):
            os.environ[env_name] = value
    return True, ""


def run_checkin():
    """调用签到脚本，返回 (code, msg, ok_count, fail_count)。"""
    module = importlib.import_module("trae_checkin")
    results, msg = module.main()
    ok = sum(1 for r in results if r.get("status") == "签到成功")
    already = sum(1 for r in results if r.get("status") == "已签到")
    pending = sum(1 for r in results if r.get("status") == "待重试")
    fail = sum(1 for r in results if r.get("status") in HARD_FAIL)
    total = len(results)
    # 全部账号都硬失败、或一个账号都没处理 → 失败；软性限流（待重试）不算失败
    code = 1 if (total == 0 or (ok + already + pending) == 0) else 0
    return code, msg, ok + already, fail


def run_credit():
    """调用积分查询脚本，返回 (code, msg, ok_count, fail_count)。"""
    module = importlib.import_module("trae_credit_monitor")
    result = module.main()
    if not isinstance(result, dict):
        return 0, "积分查询完成", 0, 0
    ok = int(result.get("ok") or 0)
    fail = int(result.get("fail") or 0)
    code = 1 if (result.get("hard_fail") or ok == 0) else 0
    return code, str(result.get("msg") or ""), ok, fail


def pause_before_exit(env, seconds=5):
    """手动运行时暂停：倒计时结束后再退出，方便查看命令行结果。"""
    if env.get("invoke_mode") != "manual":
        return
    for remain in range(seconds, 0, -1):
        print("\r窗口将在 %d 秒后自动关闭..." % remain, end="", flush=True)
        time.sleep(1)
    print()


def write_envelope(env, payload):
    """定时任务等场景把结果写入 output_json，供盒子记录。"""
    output_json = env.get("output_json")
    if not output_json:
        return
    try:
        with open(output_json, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except Exception as exc:  # noqa: BLE001
        print("⚠️  [信封] 写入失败: %s" % exc, file=sys.stderr)


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)   # 进度逐行实时刷出
    except Exception:
        pass

    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        payload = json.load(fh)

    env = payload.get("environment", {})
    params = payload.get("params", {})
    action = str(params.get("action") or "checkin").strip().lower() or "checkin"

    apply_params(params)

    code, msg, ok_count, fail_count = 0, "", 0, 0
    # 积分查询由脚本自身打印表头/进度/结果，适配器不再重复打印汇总
    show_summary = action != "credit"
    try:
        ready, reason = autofill_accounts_from_local()
        if not ready:
            code, msg, show_summary = 1, reason, True
        elif action == "credit":
            code, msg, ok_count, fail_count = run_credit()
        else:
            code, msg, ok_count, fail_count = run_checkin()
    except SystemExit as exc:
        # 上游脚本缺凭据时会 sys.exit，这里兜住转成失败信封
        code = int(exc.code) if isinstance(exc.code, int) else 1
        code = 1 if code == 0 else code
        msg = msg or "脚本因缺少账号配置而退出，请先登录 Trae 客户端"
        show_summary = True
    except Exception as exc:  # noqa: BLE001
        code = 1
        msg = "执行异常: %s" % exc
        show_summary = True

    if msg and show_summary:
        print("\n" + msg)

    envelope = {
        "code": code,
        "msg": msg or ("执行完成" if code == 0 else "执行失败"),
        "action": action,
        "ok_count": ok_count,
        "fail_count": fail_count,
    }
    write_envelope(env, envelope)
    pause_before_exit(env)


if __name__ == "__main__":
    main()
