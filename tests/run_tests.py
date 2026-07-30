"""回归测试运行器 —— 每次改代码后跑一遍，全绿才算没改坏。

用法: python tests/run_tests.py          # 只跑单元测试（不需要网络）
      python tests/run_tests.py --all    # 跑全部（含需要网络的搜索测试）
"""
import json
import re
import sys
import os
from pathlib import Path

# 确保能 import server 中的函数
sys.path.insert(0, str(Path(__file__).parent.parent))

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}  {detail}")

def run_unit_tests():
    """纯函数单元测试，不需要网络，跑得快。"""
    global PASS, FAIL
    PASS = FAIL = 0

    from server import (
        is_answer_file, extract_grade_from_name, extract_number_prefix,
        name_contains_keyword, pair_files, scan_copy_violations, _compose_copy
    )

    # 函数名 → 实际函数的映射
    FUNC_MAP = {
        "is_answer_file": is_answer_file,
        "extract_grade_from_name": extract_grade_from_name,
        "extract_number_prefix": extract_number_prefix,
        "name_contains_keyword": name_contains_keyword,
        "pair_files": pair_files,
        "scan_copy_violations": scan_copy_violations,
        "_compose_copy": _compose_copy,
    }

    test_file = Path(__file__).parent / "test_cases.json"
    with open(test_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("\n" + "=" * 60)
    print("  单元测试（纯函数，不需要网络）")
    print("=" * 60)

    for unit in data.get("unit_tests", []):
        func_name = unit["function"]
        print(f"\n📋 {unit['id']}: {func_name}")

        fn = FUNC_MAP.get(func_name)
        if fn is None:
            print(f"  ⚠️  函数 {func_name} 未找到")
            continue

        for i, case in enumerate(unit["cases"]):
            if isinstance(case["input"], list):
                result = fn(case["input"])
            elif isinstance(case["input"], dict):
                result = fn(**case["input"])
            else:
                result = fn(case["input"])

            # 特殊检查逻辑（check 字段优先）
            if case.get("check") == "first_pair_has_answer":
                ok = len(result) > 0 and result[0].get("answer") is not None
                check(f"case {i+1}: 配对成功", ok, f"实际={str(result)[:100]}")
            elif isinstance(case["expect"], dict):
                # 字典类型的期望：支持多种检查逻辑
                if "must_contain" in case["expect"] or "must_not_contain" in case["expect"]:
                    # _compose_copy 等字符串输出检查
                    must_list = case["expect"].get("must_contain", "")
                    must_not_list = case["expect"].get("must_not_contain", "")
                    if isinstance(must_list, str):
                        must_list = [must_list]
                    if isinstance(must_not_list, str):
                        must_not_list = [must_not_list]
                    ok = True
                    for must in must_list:
                        if must not in result:
                            check(f"case {i+1}: 应含「{must}」", False, f"实际={str(result)[:50]}…")
                            ok = False
                    for must_not in must_not_list:
                        if must_not in result:
                            check(f"case {i+1}: 不应含「{must_not}」", False, f"实际={str(result)[:50]}…")
                            ok = False
                    if ok:
                        check(f"case {i+1}", True)
                elif "clean" in case["expect"]:
                    # scan_copy_violations 检查
                    ok = result.get("clean") == case["expect"]["clean"]
                    if "min_violations" in case["expect"]:
                        ok = ok and len(result.get("violations", [])) >= case["expect"]["min_violations"]
                    check(f"case {i+1}", ok, f"期望 clean={case['expect']['clean']} 实际={result}")
                else:
                    ok = result == case["expect"]
                    check(f"case {i+1}", ok, f"期望={case['expect']} 实际={result}")
            else:
                ok = result == case["expect"]
                check(f"case {i+1}", ok, f"期望={case['expect']} 实际={result}")

    print(f"\n{'='*60}")
    print(f"  单元测试结果: ✅ {PASS} 通过 | ❌ {FAIL} 失败")
    print(f"{'='*60}")
    return FAIL == 0

def run_search_tests():
    """搜索集成测试，需要 Flask 后端运行 + 有效的 LEXIANG_AUTH_TOKEN。"""
    global PASS, FAIL
    search_pass = 0
    search_fail = 0

    test_file = Path(__file__).parent / "test_cases.json"
    with open(test_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    print("\n" + "=" * 60)
    print("  搜索测试（需要 Flask 后端 + 网络）")
    print("=" * 60)

    import urllib.request, urllib.error
    BASE = "http://localhost:8765"

    # 先检查健康状态
    try:
        req = urllib.request.Request(f"{BASE}/api/health")
        with urllib.request.urlopen(req, timeout=5) as resp:
            health = json.loads(resp.read().decode())
        print(f"  ✅ Flask 服务在线: {health.get('time', '')}")
    except Exception as e:
        print(f"  ❌ Flask 服务未启动: {e}")
        print(f"  💡 请先运行: python server.py")
        return False

    for tc in data["test_cases"]:
        if tc.get("input_type") == "function_test":
            continue  # 跳过函数测试，已在单元测试覆盖

        tc_id = tc["id"]
        tc_name = tc["name"]
        inp = tc["input"]
        exp = tc["expect"]

        print(f"\n📋 {tc_id}: {tc_name}")
        print(f"   输入: grade={inp['grade']} subject={inp['subject']} keyword={inp['keyword']}")

        try:
            body = json.dumps(inp).encode("utf-8")
            req = urllib.request.Request(
                f"{BASE}/api/search",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read().decode())
        except Exception as e:
            print(f"  ❌ 请求失败: {e}")
            search_fail += 1
            continue

        if not result.get("success"):
            print(f"  ❌ API 返回错误: {result.get('error', '未知')}")
            search_fail += 1
            continue

        pairs = result.get("pairs", [])
        files = [p["material"]["name"] for p in pairs]
        print(f"   返回: {len(files)} 条")

        # 检查最小文件数
        min_ok = len(files) >= exp.get("min_files", 1)
        check(f"最少 {exp.get('min_files', 1)} 条", min_ok, f"实际 {len(files)}")
        if not min_ok:
            search_fail += 1
            continue

        # 检查学科缩写
        if "all_have_subject_abbr" in exp:
            abbr = exp["all_have_subject_abbr"]
            ok = all(f"【{abbr}】" in f for f in files)
            check(f"全部含【{abbr}】", ok, f"不匹配: {[f for f in files if f'【{abbr}】' not in f][:3]}")

        # 检查年级
        if "all_have_grade" in exp:
            g = exp["all_have_grade"]
            ok = all(g in f for f in files)
            check(f"全部含年级「{g}」", ok, f"不匹配: {[f for f in files if g not in f][:3]}")

        # 检查禁止缩写
        if "forbidden_abbr" in exp:
            for fabbr in exp["forbidden_abbr"]:
                violations = [f for f in files if f"【{fabbr}】" in f]
                ok = len(violations) == 0
                check(f"禁止出现【{fabbr}】", ok, f"违禁: {violations}")

        # 检查禁止年级
        if "forbidden_grades" in exp:
            for fg in exp["forbidden_grades"]:
                violations = [f for f in files if fg in f]
                ok = len(violations) == 0
                check(f"禁止出现年级「{fg}」", ok, f"违禁: {violations}")

        search_pass += 1

    print(f"\n{'='*60}")
    print(f"  搜索测试结果: ✅ {search_pass} 通过 | ❌ {search_fail} 失败")
    print(f"{'='*60}")
    return search_fail == 0

if __name__ == "__main__":
    unit_ok = run_unit_tests()

    if "--all" in sys.argv:
        search_ok = run_search_tests()
        all_ok = unit_ok and search_ok
    else:
        print("\n💡 跳过搜索测试（需要 Flask 后端运行）。加 --all 跑全部。")
        all_ok = unit_ok

    print(f"\n{'🎉 全部通过！' if all_ok else '🔴 有失败，请检查上面的 ❌ 标记'}")
    sys.exit(0 if all_ok else 1)
