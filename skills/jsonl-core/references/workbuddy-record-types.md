---
description: WorkBuddy tree-structured conversation format and hash-chain audit log reference
---

# WorkBuddy 记录类型参考

## 核心特点

WorkBuddy 使用树形对话结构，每个消息节点都包含 `parentId` 字段指向父消息，形成对话树。这与 Claude Code 的线性会话结构不同。

## 消息节点结构

```json
{
  "id": "msg_123",
  "parentId": "msg_122",  // 指向父消息，根消息为 null
  "content": "用户消息内容",
  "role": "user",  // 或 "assistant"
  "timestamp": "2026-06-30T15:30:00.000Z",
  "hash": "sha256:abc123def456...",  // 用于审计验证的哈希
  "children": ["msg_124", "msg_125"],  // 子消息 ID 列表
  "metadata": {
    "model": "workbuddy-v2",
    "tokenCount": 150,
    "executionTime": 2.3
  }
}
```

## 审计哈希链

WorkBuddy 使用哈希链确保数据完整性：

1. **消息哈希**：每个消息的哈希基于内容 + 父消息哈希计算
2. **链式验证**：从根消息开始，验证每条消息的哈希链完整性
3. **篡改检测**：任何消息内容的修改都会破坏哈希链

**哈希计算公式**：
```
message_hash = sha256(parent_hash + content + timestamp + role)
```

**验证流程**：
```python
def verify_hash_chain(messages):
    """验证消息哈希链完整性"""
    for i, msg in enumerate(messages):
        if i == 0:  # 根消息
            expected_hash = sha256(msg['content'] + msg['timestamp'] + msg['role'])
        else:
            parent_hash = messages[i-1]['hash']
            expected_hash = sha256(parent_hash + msg['content'] + msg['timestamp'] + msg['role'])
        
        if msg['hash'] != expected_hash:
            return False, f"消息 {msg['id']} 哈希验证失败"
    return True, "哈希链完整"
```

## 树形结构遍历

### 深度优先遍历
```python
def traverse_dfs(root_id, messages):
    """深度优先遍历对话树"""
    result = []
    stack = [root_id]
    
    while stack:
        msg_id = stack.pop()
        msg = messages[msg_id]
        result.append(msg)
        
        # 子消息逆序入栈，保证左到右遍历
        for child_id in reversed(msg.get('children', [])):
            stack.append(child_id)
    
    return result
```

### 广度优先遍历
```python
def traverse_bfs(root_id, messages):
    """广度优先遍历对话树"""
    from collections import deque
    
    result = []
    queue = deque([root_id])
    
    while queue:
        msg_id = queue.popleft()
        msg = messages[msg_id]
        result.append(msg)
        
        for child_id in msg.get('children', []):
            queue.append(child_id)
    
    return result
```

## 对话分支

WorkBuddy 支持对话分支，类似于 Git 分支：

**分支创建场景**：
1. **用户编辑消息**：用户修改之前的消息，创建新分支
2. **并行工具调用**：多个工具同时执行，形成分支
3. **重试机制**：失败后的重试创建新分支

**分支标记**：
```json
{
  "id": "msg_126",
  "parentId": "msg_122",
  "branch": "retry-1",  // 分支标识
  "isRetry": true,
  "retryOf": "msg_123",  // 原始消息 ID
  "content": "重试的内容"
}
```

## 记录类型

### 消息记录
- **user**: 用户输入
- **assistant**: 助手响应
- **system**: 系统消息（工具结果、错误等）

### 元数据记录
- **session_start**: 会话开始标记
- **session_end**: 会话结束标记
- **branch_point**: 分支点标记

### 审计记录
- **hash_chain**: 哈希链验证结果
- **integrity_check**: 完整性检查记录

## 与 Claude Code 格式的对比

| 特性 | Claude Code | WorkBuddy |
|------|-------------|-----------|
| 结构 | 线性序列 | 树形结构 |
| 消息链接 | parentUuid | parentId + children |
| 完整性验证 | 无 | 哈希链 |
| 分支支持 | 有限（子代理） | 原生支持 |
| 审计日志 | 无 | 内置哈希链 |

## 解析注意事项

1. **遍历顺序**：必须遍历树形结构，不能简单线性读取
2. **哈希验证**：分析前验证哈希链完整性
3. **分支处理**：识别并处理对话分支
4. **根消息查找**：通过 `parentId: null` 找到根消息

## Session-digger 集成

在 session-digger 中解析 WorkBuddy 会话时：

```python
# 检测 WorkBuddy 格式
def detect_workbuddy_format(session_path):
    """检测是否为 WorkBuddy 格式"""
    with open(session_path) as f:
        first_line = json.loads(f.readline())
    return 'parentId' in first_line and 'hash' in first_line

# 转换为统一格式
def convert_to_universal(workbuddy_messages):
    """将 WorkBuddy 格式转换为 session-digger 统一格式"""
    universal_messages = []
    
    for msg in workbuddy_messages:
        universal_msg = {
            'uuid': msg['id'],
            'parentUuid': msg.get('parentId'),
            'type': msg['role'],
            'timestamp': msg['timestamp'],
            'message': {
                'role': msg['role'],
                'content': msg['content']
            }
        }
        universal_messages.append(universal_msg)
    
    return universal_messages
```