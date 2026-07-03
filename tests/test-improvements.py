#!/usr/bin/env python3
"""
测试 session-digger 改进的脚本
验证三个发现的改进措施
"""

import json
import os
import sys
from pathlib import Path

# 添加插件根目录到路径
PLUGIN_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

def test_codex_agents_md():
    """测试 Codex AGENTS.md 改进"""
    codex_agents_path = Path.home() / ".codex" / "AGENTS.md"
    
    if not codex_agents_path.exists():
        print("❌ Codex AGENTS.md 文件不存在")
        return False
    
    content = codex_agents_path.read_text(encoding="utf-8")
    
    # 检查是否包含关键规则
    required_rules = [
        "记忆优先",
        "从错误中学习",
        "决策追踪",
        "文件历史意识",
        "记忆维护"
    ]
    
    missing_rules = []
    for rule in required_rules:
        if rule not in content:
            missing_rules.append(rule)
    
    if missing_rules:
        print(f"❌ Codex AGENTS.md 缺少规则: {missing_rules}")
        return False
    
    print("✅ Codex AGENTS.md 改进验证通过")
    return True

def test_workbuddy_recall_agent():
    """测试 WorkBuddy 树形结构文档"""
    recall_path = PLUGIN_ROOT / "agents" / "recall.md"
    
    if not recall_path.exists():
        print("❌ recall.md 文件不存在")
        return False
    
    content = recall_path.read_text(encoding="utf-8")
    
    # 检查是否包含 WorkBuddy 树形结构说明
    if "WorkBuddy 树形会话结构" not in content:
        print("❌ recall.md 缺少 WorkBuddy 树形结构说明")
        return False
    
    if "parentId" not in content:
        print("❌ recall.md 缺少 parentId 字段说明")
        return False
    
    print("✅ WorkBuddy 树形结构文档验证通过")
    return True

def test_workbuddy_reference():
    """测试 WorkBuddy 记录类型参考文件"""
    workbuddy_ref_path = PLUGIN_ROOT / "skills" / "jsonl-core" / "references" / "workbuddy-record-types.md"
    
    if not workbuddy_ref_path.exists():
        print("❌ WorkBuddy 记录类型参考文件不存在")
        return False
    
    content = workbuddy_ref_path.read_text(encoding="utf-8")
    
    # 检查关键内容
    required_sections = [
        "树形结构遍历",
        "审计哈希链",
        "对话分支",
        "与 Claude Code 格式的对比"
    ]
    
    missing_sections = []
    for section in required_sections:
        if section not in content:
            missing_sections.append(section)
    
    if missing_sections:
        print(f"❌ WorkBuddy 参考文件缺少部分: {missing_sections}")
        return False
    
    print("✅ WorkBuddy 记录类型参考文件验证通过")
    return True

def test_trae_cn_mapping():
    """测试 Trae CN 映射文件"""
    trae_mapping_path = PLUGIN_ROOT / "skills" / "experience-synthesis" / "references" / "trae-cn-learned-mapping.md"
    
    if not trae_mapping_path.exists():
        print("❌ Trae CN 映射文件不存在")
        return False
    
    content = trae_mapping_path.read_text(encoding="utf-8")
    
    # 检查关键映射
    required_mappings = [
        "technical_implementation",
        "debugging_experience",
        "architecture_decision",
        "performance_optimization",
        "workflow"
    ]
    
    missing_mappings = []
    for mapping in required_mappings:
        if mapping not in content:
            missing_mappings.append(mapping)
    
    if missing_mappings:
        print(f"❌ Trae CN 映射文件缺少映射: {missing_mappings}")
        return False
    
    print("✅ Trae CN 映射文件验证通过")
    return True

def test_combo_map():
    """测试 combo_map.json 更新"""
    combo_map_path = PLUGIN_ROOT / "combo_map.json"
    
    if not combo_map_path.exists():
        print("❌ combo_map.json 文件不存在")
        return False
    
    try:
        with open(combo_map_path, 'r', encoding='utf-8') as f:
            combo_map = json.load(f)
    except json.JSONDecodeError as e:
        print(f"❌ combo_map.json 格式错误: {e}")
        return False
    
    # 检查 env-adapters 是否在 combos 中
    if "env-adapters" in combo_map.get("combos", {}):
        print("❌ combo_map.json 仍有 env-adapters（已合并入 echolib）")
        return False

    # 检查版本是否更新
    if combo_map.get("version") != "0.6.0":
        print(f"❌ combo_map.json 版本不正确: {combo_map.get('version')}")
        return False
    
    print("✅ combo_map.json 更新验证通过")
    return True

def test_jsonl_core_skill():
    """测试 jsonl-core 技能更新"""
    jsonl_core_path = PLUGIN_ROOT / "skills" / "jsonl-core" / "SKILL.md"
    
    if not jsonl_core_path.exists():
        print("❌ jsonl-core SKILL.md 文件不存在")
        return False
    
    content = jsonl_core_path.read_text(encoding="utf-8")
    
    # 检查是否引用了 WorkBuddy 参考文件
    if "workbuddy-record-types.md" not in content:
        print("❌ jsonl-core SKILL.md 缺少 WorkBuddy 参考文件引用")
        return False
    
    print("✅ jsonl-core 技能更新验证通过")
    return True

def main():
    """运行所有测试"""
    print("🧪 开始验证 session-digger 改进措施...")
    print()
    
    tests = [
        test_codex_agents_md,
        test_workbuddy_recall_agent,
        test_workbuddy_reference,
        test_trae_cn_mapping,
        test_combo_map,
        test_jsonl_core_skill,
    ]
    
    passed = 0
    failed = 0
    
    for test in tests:
        try:
            if test():
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"❌ 测试 {test.__name__} 抛出异常: {e}")
            failed += 1
        print()
    
    print(f"📊 测试结果: {passed} 通过, {failed} 失败")
    
    if failed == 0:
        print("🎉 所有改进措施验证通过！")
        return 0
    else:
        print("⚠️  部分改进措施需要修复")
        return 1

if __name__ == "__main__":
    sys.exit(main())