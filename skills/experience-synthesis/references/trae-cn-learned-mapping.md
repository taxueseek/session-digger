---
description: Mapping Trae CN's "learned" categories to session-digger's insight taxonomy
---

# Trae CN "Learned" 类别到洞察分类法的映射

## 概述

Trae CN 存储结构化摘要：intent→actions→outcome→learned。这些 "learned" 项目形成隐式知识图谱。本文档将 Trae CN 的 "learned" 类别映射到 session-digger 的洞察分类法。

## Trae CN 的 "learned" 类别

Trae CN 通常包含以下类型的 "learned" 项目：

### 1. **技术实现 learned**
- **描述**：关于如何实现特定技术功能的经验
- **示例**：「使用 WebSocket 实现实时通信时，需要处理连接断开重连」
- **数据格式**：
  ```json
  {
    "category": "technical_implementation",
    "content": "WebSocket 重连需要指数退避算法",
    "context": "实时聊天应用",
    "frequency": 3,
    "confidence": 0.85
  }
  ```

### 2. **调试经验 learned**
- **描述**：调试问题的经验和技巧
- **示例**：「当 API 返回 500 错误时，首先检查数据库连接池配置」
- **数据格式**：
  ```json
  {
    "category": "debugging_experience",
    "content": "500 错误优先检查数据库连接",
    "context": "后端服务调试",
    "frequency": 5,
    "confidence": 0.92
  }
  ```

### 3. **架构决策 learned**
- **描述**：架构设计决策的经验
- **示例**：「微服务架构下，服务间通信使用 gRPC 比 REST 更高效」
- **数据格式**：
  ```json
  {
    "category": "architecture_decision",
    "content": "微服务间通信推荐 gRPC",
    "context": "系统架构设计",
    "frequency": 2,
    "confidence": 0.78
  }
  ```

### 4. **性能优化 learned**
- **描述**：性能优化经验
- **示例**：「数据库查询优化：为经常查询的字段添加索引」
- **数据格式**：
  ```json
  {
    "category": "performance_optimization",
    "content": "为查询字段添加索引",
    "context": "数据库性能优化",
    "frequency": 4,
    "confidence": 0.88
  }
  ```

### 5. **工作流程 learned**
- **描述**：开发工作流程经验
- **示例**：「使用 Git 分支策略：功能分支 → 开发分支 → 主分支」
- **数据格式**：
  ```json
  {
    "category": "workflow",
    "content": "Git 分支策略：feature → develop → main",
    "context": "版本控制",
    "frequency": 6,
    "confidence": 0.95
  }
  ```

## 映射到 Session-digger 洞察分类法

### 映射表

| Trae CN 类别 | Session-digger 类别 | 映射理由 | 置信度 |
|-------------|----------------|----------|--------|
| technical_implementation | Effective Patterns (Sub-type: Implementation pattern) | 技术实现经验属于有效模式 | 高 |
| debugging_experience | Effective Patterns (Sub-type: Debugging pattern) | 调试经验属于有效模式 | 高 |
| architecture_decision | Architecture Knowledge | 架构决策直接对应架构知识 | 高 |
| performance_optimization | Performance & Cost Patterns | 性能优化直接对应性能模式 | 高 |
| workflow | User Preferences (Sub-type: Workflow preferences) | 工作流程属于用户偏好 | 中 |
| error_handling | Mistakes & Corrections | 错误处理经验来自错误修正 | 中 |
| tool_usage | User Preferences (Sub-type: Tool preferences) | 工具使用属于用户偏好 | 中 |

### 详细映射规则

#### 1. technical_implementation → Effective Patterns
**转换逻辑**：
```python
def map_technical_implementation(trae_learned):
    """将技术实现 learned 映射到有效模式"""
    return {
        'category': 'Effective Patterns',
        'subcategory': 'Implementation pattern',
        'insight': trae_learned['content'],
        'context': trae_learned['context'],
        'evidence': f"Trae CN 学习记录，频率: {trae_learned['frequency']}",
        'action': f"在类似场景中应用: {trae_learned['content']}"
    }
```

#### 2. debugging_experience → Effective Patterns
**转换逻辑**：
```python
def map_debugging_experience(trae_learned):
    """将调试经验 learned 映射到有效模式"""
    return {
        'category': 'Effective Patterns',
        'subcategory': 'Debugging pattern',
        'insight': trae_learned['content'],
        'context': trae_learned['context'],
        'evidence': f"Trae CN 学习记录，频率: {trae_learned['frequency']}",
        'action': f"遇到类似问题时: {trae_learned['content']}"
    }
```

#### 3. architecture_decision → Architecture Knowledge
**转换逻辑**：
```python
def map_architecture_decision(trae_learned):
    """将架构决策 learned 映射到架构知识"""
    return {
        'category': 'Architecture Knowledge',
        'subcategory': 'Tech stack',
        'insight': trae_learned['content'],
        'context': trae_learned['context'],
        'evidence': f"Trae CN 学习记录，置信度: {trae_learned['confidence']}",
        'action': f"架构设计时参考: {trae_learned['content']}"
    }
```

#### 4. performance_optimization → Performance & Cost Patterns
**转换逻辑**：
```python
def map_performance_optimization(trae_learned):
    """将性能优化 learned 映射到性能模式"""
    return {
        'category': 'Performance & Cost Patterns',
        'subcategory': 'Optimization',
        'insight': trae_learned['content'],
        'context': trae_learned['context'],
        'evidence': f"Trae CN 学习记录，频率: {trae_learned['frequency']}",
        'action': f"性能优化时应用: {trae_learned['content']}"
    }
```

#### 5. workflow → User Preferences
**转换逻辑**：
```python
def map_workflow(trae_learned):
    """将工作流程 learned 映射到用户偏好"""
    return {
        'category': 'User Preferences',
        'subcategory': 'Workflow preferences',
        'insight': trae_learned['content'],
        'context': trae_learned['context'],
        'evidence': f"Trae CN 学习记录，频率: {trae_learned['frequency']}",
        'action': f"工作流程中采用: {trae_learned['content']}"
    }
```

## 批量转换脚本

```python
import json
from typing import List, Dict

def convert_trae_learned_to_insights(trae_learned_list: List[Dict]) -> List[Dict]:
    """批量转换 Trae CN learned 为 session-digger 洞察"""
    conversion_map = {
        'technical_implementation': map_technical_implementation,
        'debugging_experience': map_debugging_experience,
        'architecture_decision': map_architecture_decision,
        'performance_optimization': map_performance_optimization,
        'workflow': map_workflow
    }
    
    insights = []
    for learned in trae_learned_list:
        category = learned.get('category', '')
        if category in conversion_map:
            insight = conversion_map[category](learned)
            insights.append(insight)
        else:
            # 未知类别，作为通用经验处理
            insights.append({
                'category': 'Effective Patterns',
                'subcategory': 'General',
                'insight': learned.get('content', ''),
                'context': learned.get('context', ''),
                'evidence': f"Trae CN 学习记录，类别: {category}",
                'action': learned.get('content', '')
            })
    
    return insights

def merge_with_existing_insights(new_insights: List[Dict], existing_insights: List[Dict]) -> List[Dict]:
    """合并新洞察与现有洞察，去重并更新频率"""
    merged = {insight['insight']: insight for insight in existing_insights}
    
    for new_insight in new_insights:
        key = new_insight['insight']
        if key in merged:
            # 更新频率和置信度
            existing = merged[key]
            if 'frequency' in new_insight.get('evidence', ''):
                freq = int(new_insight['evidence'].split('频率: ')[1])
                existing_freq = int(existing.get('evidence', '频率: 0').split('频率: ')[1]) if '频率: ' in existing.get('evidence', '') else 0
                existing['evidence'] = existing['evidence'].replace(f'频率: {existing_freq}', f'频率: {existing_freq + freq}')
        else:
            merged[key] = new_insight
    
    return list(merged.values())
```

## 集成到 Session-digger 工作流

### 在 experience-synthesis 中使用
```python
# 在 experience-synthesis 技能中添加 Trae CN 支持
def process_trae_cn_sessions(session_paths):
    """处理 Trae CN 会话，提取 learned 项目"""
    all_learned = []
    
    for session_path in session_paths:
        # 检测是否为 Trae CN 格式
        if detect_trae_cn_format(session_path):
            learned = extract_trae_cn_learned(session_path)
            all_learned.extend(learned)
    
    # 转换为 session-digger 洞察格式
    insights = convert_trae_learned_to_insights(all_learned)
    
    return insights
```

### 在 recall agent 中使用
```bash
# 搜索 Trae CN 会话中的 learned 项目
bash ${CLAUDE_PLUGIN_ROOT}/scripts/list-sessions.sh "all" --agent trae --grep "learned" --limit 20
```

## 质量评估

### 映射质量指标
1. **覆盖率**：Trae CN learned 项目被映射的百分比
2. **准确性**：映射后类别的正确性
3. **实用性**：映射后洞察的可操作性

### 改进建议
1. **机器学习增强**：使用 NLP 模型自动分类未识别的 learned 类别
2. **上下文扩展**：结合会话上下文丰富 learned 项目的背景信息
3. **频率权重**：根据 learned 项目的出现频率调整洞察的优先级