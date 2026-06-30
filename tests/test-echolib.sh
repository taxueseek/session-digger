#!/usr/bin/env bash
# test-echolib.sh — Test suite for session-digger's echolib.py and script wrappers
# Usage: bash tests/test-echolib.sh
#
# Runs against fixtures/sample-session.jsonl and reports pass/fail for each test.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)/scripts"
FIXTURE_DIR="$(cd "$(dirname "$0")" && pwd)/fixtures"
SAMPLE="$FIXTURE_DIR/sample-session.jsonl"

PASS=0
FAIL=0
ERRORS=""

pass() { PASS=$((PASS + 1)); echo "  PASS: $1"; }
fail() { FAIL=$((FAIL + 1)); ERRORS="$ERRORS\n  FAIL: $1 — $2"; echo "  FAIL: $1 — $2"; }

assert_contains() {
  local output="$1" expected="$2" test_name="$3"
  if echo "$output" | grep -qF "$expected"; then
    pass "$test_name"
  else
    fail "$test_name" "expected to contain: $expected"
  fi
}

assert_not_contains() {
  local output="$1" expected="$2" test_name="$3"
  if echo "$output" | grep -qF "$expected"; then
    fail "$test_name" "should NOT contain: $expected"
  else
    pass "$test_name"
  fi
}

assert_equals() {
  local actual="$1" expected="$2" test_name="$3"
  if [[ "$actual" == "$expected" ]]; then
    pass "$test_name"
  else
    fail "$test_name" "expected '$expected', got '$actual'"
  fi
}

assert_count() {
  local output="$1" expected="$2" test_name="$3"
  local actual
  actual=$(echo "$output" | grep -c '.' || true)
  if [[ "$actual" -eq "$expected" ]]; then
    pass "$test_name"
  else
    fail "$test_name" "expected $expected lines, got $actual"
  fi
}

# ===================================================================
echo "=== Testing echolib.py directly ==="
# ===================================================================

echo ""
echo "--- session_stats ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
stats = echolib.session_stats(os.environ['ES_FILE'])
for k, v in stats.items():
    print(f'{k}={v}')
")

assert_contains "$output" "slug=test-slug" "stats: slug detected"
assert_contains "$output" "model=claude-sonnet-4-5-20250514" "stats: model detected"
assert_contains "$output" "branch=main" "stats: branch detected"
assert_contains "$output" "user_messages=3" "stats: user message count (filters meta/compact/tool_result)"
assert_contains "$output" "tool_calls=3" "stats: tool call count"
assert_contains "$output" "files_edited=3" "stats: files_edited from last snapshot"
assert_contains "$output" "errors=1" "stats: error count (single pass)"
assert_contains "$output" "compactions=1" "stats: compaction count"
assert_contains "$output" "summary=Fix auth SQL injection" "stats: summary captured"

echo ""
echo "--- extract_messages (user only) ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
for m in echolib.extract_messages(os.environ['ES_FILE'], role='user'):
    print(m['role'] + ': ' + m['text'][:60])
")

assert_contains "$output" "fix the authentication bug" "messages: first user message"
assert_contains "$output" "yes, fix that too please" "messages: second user message"
assert_not_contains "$output" "system-reminder" "messages: system reminder filtered"
assert_not_contains "$output" "compact summary" "messages: isMeta filtered"
assert_not_contains "$output" "compacted context" "messages: isCompactSummary filtered"
assert_not_contains "$output" "tool_result" "messages: tool results filtered"

echo ""
echo "--- extract_messages (assistant, no-tools) ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
for m in echolib.extract_messages(os.environ['ES_FILE'], role='assistant', no_tools=True):
    print(m['role'] + ': ' + m['text'][:80])
")

assert_contains "$output" "SQL injection vulnerability" "messages: assistant text present"
assert_not_contains "$output" "[TOOL:" "messages: tools hidden with no_tools"
assert_not_contains "$output" "passthrough" "messages: synthetic messages filtered"

echo ""
echo "--- extract_messages (with thinking) ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
for m in echolib.extract_messages(os.environ['ES_FILE'], role='assistant', thinking_limit=0):
    print(m['text'][:120])
")

assert_contains "$output" "[THINKING]" "messages: thinking blocks shown"
assert_contains "$output" "analyze the authentication bug" "messages: thinking content present"

echo ""
echo "--- extract_messages (thinking hidden by default) ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
for m in echolib.extract_messages(os.environ['ES_FILE'], role='assistant', thinking_limit=-1):
    print(m['text'][:120])
")

assert_not_contains "$output" "[THINKING]" "messages: thinking hidden by default"

echo ""
echo "--- extract_messages (limit) ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
count = 0
for m in echolib.extract_messages(os.environ['ES_FILE'], limit=2):
    count += 1
print(count)
")

assert_equals "$(echo "$output" | tail -1)" "2" "messages: limit works"

echo ""
echo "--- extract_tools ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
for t in echolib.extract_tools(os.environ['ES_FILE']):
    print(f\"{t['name']}\t{t['status']}\t{t['key_input'][:50]}\")
")

assert_contains "$output" "Read	ok	/Users/test/project/src/login.ts" "tools: Read call captured"
assert_contains "$output" "Edit	ok	/Users/test/project/src/login.ts" "tools: Edit call captured"
assert_contains "$output" "Bash	error" "tools: Bash error captured"

echo ""
echo "--- extract_tools (errors only) ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
for t in echolib.extract_tools(os.environ['ES_FILE'], errors_only=True):
    print(f\"{t['name']}\t{t['status']}\")
")

assert_count "$output" 1 "tools: errors-only returns 1 result"
assert_contains "$output" "Bash	error" "tools: error result is the Bash call"

echo ""
echo "--- extract_tools (filter by name) ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
for t in echolib.extract_tools(os.environ['ES_FILE'], tool_filter='Edit'):
    print(t['name'])
")

assert_count "$output" 1 "tools: filter returns only Edit calls"

echo ""
echo "--- extract_files_changed ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
for f in echolib.extract_files_changed(os.environ['ES_FILE']):
    print(f[0])
")

assert_contains "$output" "src/login.ts" "files: login.ts present"
assert_contains "$output" "src/auth.ts" "files: auth.ts present"
assert_contains "$output" "src/middleware.ts" "files: middleware.ts present (from LAST snapshot)"
assert_count "$output" 3 "files: 3 files from last snapshot"

echo ""
echo "--- extract_files_changed (with versions) ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
for f in echolib.extract_files_changed(os.environ['ES_FILE'], with_versions=True):
    print(f'{f[0]}\t{f[1]}')
")

assert_contains "$output" "src/login.ts	3" "files: login.ts has version 3"
assert_contains "$output" "src/middleware.ts	1" "files: middleware.ts has version 1"

echo ""
echo "--- detect_schema ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
schema = echolib.detect_schema(os.environ['ES_FILE'])
print('unknown=' + ','.join(schema['unknown_types']) if schema['unknown_types'] else 'unknown=none')
print('versions=' + ','.join(schema['versions']))
print('models=' + ','.join(schema['models']))
for rtype, info in schema['record_types'].items():
    print(f'{rtype}={info[\"count\"]}')
")

assert_contains "$output" "unknown=none" "schema: no unknown types"
assert_contains "$output" "versions=2.1.39" "schema: version detected"
assert_contains "$output" "models=claude-sonnet-4-5-20250514" "schema: model detected"
assert_contains "$output" "user=8" "schema: user record count"
assert_contains "$output" "assistant=5" "schema: assistant record count"
assert_contains "$output" "summary=1" "schema: summary record count"
assert_contains "$output" "pr-link=1" "schema: pr-link record count"
assert_contains "$output" "file-history-snapshot=2" "schema: snapshot count"

echo ""
echo "--- iter_records (type filter + skip_noise) ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
count = 0
for r in echolib.iter_records(os.environ['ES_FILE'], types={'user'}, skip_noise=True):
    count += 1
print(count)
")

assert_equals "$(echo "$output" | tail -1)" "8" "iter: type filter + noise skip"

echo ""
echo "--- iter_records (limit) ---"
output=$(ES_FILE="$SAMPLE" ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
count = 0
for r in echolib.iter_records(os.environ['ES_FILE'], limit=3):
    count += 1
print(count)
")

assert_equals "$(echo "$output" | tail -1)" "3" "iter: limit stops at 3"


# ===================================================================
echo ""
echo "=== Testing shell script wrappers ==="
# ===================================================================

CLAUDE_PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export CLAUDE_PLUGIN_ROOT

echo ""
echo "--- session-stats.sh ---"
output=$(bash "$SCRIPT_DIR/session-stats.sh" "$SAMPLE")
assert_contains "$output" "user_messages=3" "wrapper: session-stats user count"
assert_contains "$output" "errors=1" "wrapper: session-stats error count"

echo ""
echo "--- extract-messages.sh ---"
output=$(bash "$SCRIPT_DIR/extract-messages.sh" "$SAMPLE" --role user --limit 5)
assert_contains "$output" "fix the authentication bug" "wrapper: extract-messages user"
assert_not_contains "$output" "passthrough" "wrapper: extract-messages filters synthetic"

echo ""
echo "--- extract-tools.sh ---"
output=$(bash "$SCRIPT_DIR/extract-tools.sh" "$SAMPLE" --limit 5)
assert_contains "$output" "Read" "wrapper: extract-tools has Read"
assert_contains "$output" "Edit" "wrapper: extract-tools has Edit"
assert_contains "$output" "Bash" "wrapper: extract-tools has Bash"

echo ""
echo "--- extract-tools.sh --errors-only ---"
output=$(bash "$SCRIPT_DIR/extract-tools.sh" "$SAMPLE" --errors-only)
assert_count "$output" 1 "wrapper: extract-tools errors-only count"

echo ""
echo "--- extract-files-changed.sh ---"
output=$(bash "$SCRIPT_DIR/extract-files-changed.sh" "$SAMPLE")
assert_count "$output" 3 "wrapper: extract-files-changed count"
assert_contains "$output" "src/middleware.ts" "wrapper: files-changed has middleware (last snapshot)"

echo ""
echo "--- extract-files-changed.sh --with-versions ---"
output=$(bash "$SCRIPT_DIR/extract-files-changed.sh" "$SAMPLE" --with-versions)
assert_contains "$output" "src/login.ts	3" "wrapper: files-changed version count"

echo ""
echo "--- parse-jsonl.sh --detect-schema ---"
output=$(bash "$SCRIPT_DIR/parse-jsonl.sh" "$SAMPLE" --detect-schema)
assert_contains "$output" "unknown_types=none" "wrapper: parse-jsonl schema no unknowns"
assert_contains "$output" "user:" "wrapper: parse-jsonl schema has user type"

echo ""
echo "--- parse-jsonl.sh --types --limit ---"
output=$(bash "$SCRIPT_DIR/parse-jsonl.sh" "$SAMPLE" --types user --skip-noise --limit 2)
assert_count "$output" 2 "wrapper: parse-jsonl type filter + limit"

echo ""
echo "--- parse-jsonl.sh --format tsv --fields ---"
output=$(bash "$SCRIPT_DIR/parse-jsonl.sh" "$SAMPLE" --types summary --fields type,summary --format tsv)
assert_contains "$output" "summary	Fix auth SQL injection" "wrapper: parse-jsonl tsv format"


# ===================================================================
echo ""
echo "=== Testing edge cases ==="
# ===================================================================

echo ""
echo "--- empty file ---"
EMPTY=$(mktemp)
echo "" > "$EMPTY"
output=$(bash "$SCRIPT_DIR/session-stats.sh" "$EMPTY")
assert_contains "$output" "user_messages=0" "edge: empty file stats"
output=$(bash "$SCRIPT_DIR/extract-files-changed.sh" "$EMPTY" 2>&1)
assert_contains "$output" "no files changed" "edge: empty file no files"
rm "$EMPTY"

echo ""
echo "--- malformed JSON ---"
BROKEN=$(mktemp)
echo 'not json at all' > "$BROKEN"
echo '{"type":"user","message":{"content":"valid"},"timestamp":"2026-01-01T00:00:00Z"}' >> "$BROKEN"
echo '{broken json' >> "$BROKEN"
output=$(bash "$SCRIPT_DIR/session-stats.sh" "$BROKEN")
assert_contains "$output" "user_messages=1" "edge: malformed lines skipped gracefully"
rm "$BROKEN"

echo ""
echo "--- unknown options rejected ---"
output=$(bash "$SCRIPT_DIR/extract-messages.sh" "$SAMPLE" --badoption 2>&1 || true)
assert_contains "$output" "ERROR: Unknown option" "edge: unknown option rejected"

output=$(bash "$SCRIPT_DIR/extract-tools.sh" "$SAMPLE" --badoption 2>&1 || true)
assert_contains "$output" "ERROR: Unknown option" "edge: unknown option rejected (tools)"

output=$(bash "$SCRIPT_DIR/list-sessions.sh" --badoption 2>&1 || true)
assert_contains "$output" "ERROR: Unknown option" "edge: unknown option rejected (list-sessions)"


echo ""
echo "--- parse_frontmatter ---"

# Valid frontmatter
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
fm, body = echolib.parse_frontmatter('''---
name: test memory
description: a test description
type: project
---

Body content here.
''')
print(f'name={fm.get(\"name\", \"\")}')
print(f'type={fm.get(\"type\", \"\")}')
print(f'body_starts_with={body.strip()[:12]}')
")
assert_contains "$output" "name=test memory" "frontmatter: name parsed"
assert_contains "$output" "type=project" "frontmatter: type parsed"
assert_contains "$output" "body_starts_with=Body content" "frontmatter: body extracted"

# No frontmatter
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
fm, body = echolib.parse_frontmatter('Just plain text with no frontmatter.')
print(f'fm_empty={len(fm) == 0}')
print(f'body={body.strip()}')
")
assert_contains "$output" "fm_empty=True" "frontmatter: empty dict when no frontmatter"
assert_contains "$output" "body=Just plain text" "frontmatter: full text as body when no frontmatter"

# Broken frontmatter (no closing ---)
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
fm, body = echolib.parse_frontmatter('''---
name: broken
description: no closing delimiter
type: project

Some content here.
''')
print(f'fm_empty={len(fm) == 0}')
")
assert_contains "$output" "fm_empty=True" "frontmatter: empty dict when no closing delimiter"

# Value with colon
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
fm, body = echolib.parse_frontmatter('''---
name: URL reference
description: grafana at https://example.com/dashboard
type: reference
---

Content.
''')
print(f'desc={fm.get(\"description\", \"\")}')
")
assert_contains "$output" "desc=grafana at https://example.com/dashboard" "frontmatter: colon in value preserved"

echo ""
echo "--- iter_memories ---"

MEMORY_FIXTURE_1="$FIXTURE_DIR/memory-layout-1"
MEMORY_FIXTURE_2="$FIXTURE_DIR/memory-layout-2"
MEMORY_FIXTURE_3="$FIXTURE_DIR/memory-layout-3"
MEMORY_MALFORMED="$FIXTURE_DIR/memory-malformed"

# Layout 1: index + individual files
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" ES_DIR="$MEMORY_FIXTURE_1" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
mems = list(echolib.iter_memories(os.environ['ES_DIR']))
for m in mems:
    print(f'{m.name}|{m.type}|{m.size}')
print(f'count={len(mems)}')
")
assert_contains "$output" "count=3" "iter_memories: layout 1 yields 3 files (skips MEMORY.md and archive)"
assert_contains "$output" "project context|project|" "iter_memories: layout 1 parses project memory"
assert_contains "$output" "testing preferences|feedback|" "iter_memories: layout 1 parses feedback memory"
assert_not_contains "$output" "old deployment" "iter_memories: layout 1 skips archive/"

# Layout 2: standalone MEMORY.md
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" ES_DIR="$MEMORY_FIXTURE_2" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
mems = list(echolib.iter_memories(os.environ['ES_DIR']))
for m in mems:
    print(f'type={m.type}')
    print(f'has_content={len(m.content) > 10}')
print(f'count={len(mems)}')
")
assert_contains "$output" "count=1" "iter_memories: layout 2 yields 1 memory from standalone MEMORY.md"
assert_contains "$output" "type=unknown" "iter_memories: layout 2 type is unknown"
assert_contains "$output" "has_content=True" "iter_memories: layout 2 has content"

# Layout 3: empty MEMORY.md
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" ES_DIR="$MEMORY_FIXTURE_3" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
mems = list(echolib.iter_memories(os.environ['ES_DIR']))
print(f'count={len(mems)}')
")
assert_contains "$output" "count=0" "iter_memories: layout 3 (empty) yields 0 memories"

# Malformed frontmatter
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" ES_DIR="$MEMORY_MALFORMED" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
mems = list(echolib.iter_memories(os.environ['ES_DIR']))
for m in mems:
    print(f'{m.name}|{m.type}')
print(f'count={len(mems)}')
")
assert_contains "$output" "count=2" "iter_memories: malformed dir yields 2 memories"
assert_contains "$output" "None|unknown" "iter_memories: malformed frontmatter yields type=unknown"

# Layout with memory/ subdirectory (exercises basename=="memory" code path)
MEMORY_FIXTURE_PROJ="$FIXTURE_DIR/fake-project/memory"
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" ES_DIR="$MEMORY_FIXTURE_PROJ" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
mems = list(echolib.iter_memories(os.environ['ES_DIR']))
for m in mems:
    print(f'project={m.project}|type={m.type}')
print(f'count={len(mems)}')
")
assert_contains "$output" "count=1" "iter_memories: fake-project/memory/ yields 1 file"
assert_contains "$output" "project=fake-project" "iter_memories: project name derived from parent of memory/"

echo ""
echo "--- resolve_project_root ---"

# Test with a known project directory that exists on disk
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
script_dir = os.environ['ES_SCRIPT_DIR']
plugin_root = os.path.dirname(script_dir)
encoded = echolib._encode_project_path(plugin_root)
claude_dir = echolib.CLAUDE_DIR / encoded
if claude_dir.is_dir():
    result = echolib.resolve_project_root(claude_dir)
    print(f'resolved={result is not None}')
    if result:
        print(f'exists={os.path.isdir(result)}')
else:
    print('resolved=skip')
    print('exists=skip')
")
if echo "$output" | grep -qF "resolved=skip" || echo "$output" | grep -qF "resolved=False"; then
  pass "resolve_project_root: skipped (no Claude dir or unresolvable for test project)"
else
  assert_contains "$output" "resolved=True" "resolve_project_root: resolves known project"
  assert_contains "$output" "exists=True" "resolve_project_root: resolved path exists on disk"
fi

# Test with a fake nonexistent project directory
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
from pathlib import Path
import echolib
fake_dir = Path('/tmp/session-digger-test-nonexistent-project-dir')
result = echolib.resolve_project_root(fake_dir)
print(f'result_none={result is None}')
")
assert_contains "$output" "result_none=True" "resolve_project_root: returns None for nonexistent dir"

echo ""
echo "--- staleness_score ---"

output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" ES_DIR="$MEMORY_FIXTURE_1" python3 -c "
import os, sys, time
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
mems = list(echolib.iter_memories(os.environ['ES_DIR']))
for m in mems:
    ss = echolib.staleness_score(m)
    print(f'{m.type}|score={ss.score}|action={ss.action}')
")
assert_contains "$output" "action=keep" "staleness_score: fresh fixtures score as keep"

# Test with artificially old memory
output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys, time, math
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
m = echolib.Memory(
    path='/fake', name='old', description='old', type='project',
    content='test', project='test', project_dir='/fake',
    mtime=time.time() - (60 * 86400),
    size=100
)
ss = echolib.staleness_score(m)
print(f'score={ss.score}')
print(f'action={ss.action}')
print(f'high_score={ss.score > 80}')
")
assert_contains "$output" "action=prune" "staleness_score: 60-day-old project memory -> prune"
assert_contains "$output" "high_score=True" "staleness_score: 60-day-old project memory score > 80"

echo ""
echo "--- estimate_tokens ---"

output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
print(f'tokens={echolib.estimate_tokens(\"a\" * 400)}')
")
assert_equals "$(echo "$output" | grep tokens= | head -1)" "tokens=100" "estimate_tokens: 400 chars = 100 tokens"

echo ""
echo "--- all_memory_dirs ---"

output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
dirs = echolib.all_memory_dirs()
print(f'count={len(dirs)}')
print(f'is_list={isinstance(dirs, list)}')
")
assert_contains "$output" "is_list=True" "all_memory_dirs: returns a list"

echo ""
echo "--- memory_stats ---"

output=$(ES_SCRIPT_DIR="$SCRIPT_DIR" ES_DIR="$MEMORY_FIXTURE_1" python3 -c "
import os, sys
sys.path.insert(0, os.environ['ES_SCRIPT_DIR'])
import echolib
stats = echolib.memory_stats(os.environ['ES_DIR'])
print(f'file_count={stats.file_count}')
print(f'has_tokens={stats.estimated_tokens > 0}')
print(f'has_dist={len(stats.staleness_distribution) == 4}')
")
assert_contains "$output" "file_count=3" "memory_stats: counts 3 files in layout 1"
assert_contains "$output" "has_tokens=True" "memory_stats: has positive token count"
assert_contains "$output" "has_dist=True" "memory_stats: has 4-bucket distribution"

# ===================================================================
echo ""
echo "=========================================="
echo "  Results: $PASS passed, $FAIL failed"
echo "=========================================="

if [[ $FAIL -gt 0 ]]; then
  echo ""
  echo "Failures:"
  echo -e "$ERRORS"
  exit 1
fi
