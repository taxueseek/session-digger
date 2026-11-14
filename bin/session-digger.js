#!/usr/bin/env node
/**
 * npx / npm CLI & MCP entry for session-digger.
 * Requires Python 3.9+ (stdlib only, zero pip dependencies).
 */
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

function exitCodeOf(code, signal) {
  if (code !== null && code !== undefined) return code;
  const num = (os.constants.signals || {})[signal];
  return typeof num === 'number' ? 128 + num : 128;
}

function resolvePython() {
  if (process.env.SESSION_DIGGER_PYTHON) return process.env.SESSION_DIGGER_PYTHON;
  if (process.env.PYTHON) return process.env.PYTHON;
  return process.platform === 'win32' ? 'python' : 'python3';
}

const ROOT = path.join(__dirname, '..');
const SCRIPTS = path.join(ROOT, 'scripts');
const PYTHON = resolvePython();
const env = { ...process.env, PYTHONUTF8: process.env.PYTHONUTF8 || '1' };
const argv = process.argv.slice(2);

function runPython(scriptRel, args, stdio = 'inherit') {
  const target = path.join(SCRIPTS, scriptRel);
  if (!fs.existsSync(target)) {
    console.error(`session-digger: script not found: ${target}`);
    process.exit(1);
  }
  const proc = spawn(PYTHON, [target, ...args], { stdio, env });
  if (stdio !== 'inherit') {
    process.stdin.pipe(proc.stdin);
    proc.stdout.pipe(process.stdout);
  }
  proc.on('error', (err) => {
    console.error(`session-digger: failed to start Python (${PYTHON}): ${err.message}`);
    console.error('Python 3.9+ is required (stdlib only, no pip install needed).');
    process.exit(1);
  });
  proc.on('exit', (code, signal) => {
    process.exit(exitCodeOf(code, signal));
  });
}

if (argv[0] === '-v' || argv[0] === '--version') {
  try {
    const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, 'package.json'), 'utf8'));
    console.log(pkg.version);
  } catch (_) {
    console.log('unknown');
  }
  process.exit(0);
}

if (argv[0] === '-h' || argv[0] === '--help') {
  console.log(`session-digger — Cross-environment AI session mining & usage analytics

Usage:
  session-digger                         Start MCP stdio server (default)
  session-digger mcp                     Start MCP stdio server explicitly
  session-digger call <tool> [json]      Single-shot MCP tool invocation
  session-digger index [args...]         Build/update SQLite FTS5 index
  session-digger search <kw> [args...]   Search sessions across 20+ agents
  session-digger sessions [args...]      List recent sessions
  session-digger stats [args...]         Cross-environment stats & token usage
  session-digger reflect [args...]       Generate offline HTML usage report
  session-digger trend [args...]         Run period-over-period / regression trends
  session-digger cache-audit [args...]   Audit prompt cache hit rates
  session-digger -v, --version           Print version
  session-digger -h, --help              Show this help

Requirements: Python 3.9+ (stdlib only, zero pip dependencies).`);
  process.exit(0);
}

const sub = argv[0];
if (sub === 'call') {
  const tool = argv[1];
  if (!tool) {
    console.error('usage: session-digger call <tool> [json-args]');
    process.exit(2);
  }
  runPython('mcp_server.py', ['--call', tool, argv[2] ?? '{}']);
} else if (sub === 'search' || sub === 'sessions' || sub === 'stats') {
  runPython('sd-recall.py', argv);
} else if (sub === 'index') {
  const rest = argv.slice(1);
  runPython('index-builder.py', rest.length ? rest : ['build']);
} else if (sub === 'reflect') {
  runPython('reflect-report.py', argv.slice(1));
} else if (sub === 'trend') {
  runPython('trend-engine.py', argv.slice(1));
} else if (sub === 'cache-audit') {
  runPython('cache_audit.py', argv.slice(1));
} else {
  const forwarded = sub === 'mcp' ? argv.slice(1) : argv;
  runPython('mcp_server.py', forwarded, ['pipe', 'pipe', 'inherit']);
}
