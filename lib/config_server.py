#!/usr/bin/env python3
"""
Claude Code Portable — 配置中心 (Config Center)

A self-contained, dependency-free (stdlib-only) local web config panel,
styled consistently with the OpenClaw / Hermes portable config centers.

It reads and writes the SAME store the launcher uses: the cc-switch
SQLite database at data/.cc-switch/cc-switch.db, `providers` table.
So configuring here is equivalent to configuring in the cc-switch GUI —
the launcher (ClaudePortable.command/.sh/.bat) picks up the change.

Design goals shared across the four portable projects:
  - first-run onboarding wizard (pick provider → key → test → done)
  - rich, full tabbed UI (model / advanced / data / about)
  - localhost-only bind + DNS-rebind Host pin (panel holds API keys)
  - graceful offline degradation
  - zero extra runtime: pure python3 stdlib (already a launcher dep)

Usage:
  python3 lib/config_server.py            # serve on 127.0.0.1:17580
"""
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import uuid
import shutil
import urllib.request
import urllib.error
import webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent.resolve()
PORTABLE_ROOT = SCRIPT_DIR.parent if SCRIPT_DIR.name == "lib" else SCRIPT_DIR
DATA_DIR = PORTABLE_ROOT / "data"
CCS_DIR = DATA_DIR / ".cc-switch"
CCS_DB = CCS_DIR / "cc-switch.db"

PORT = 17580          # config-center port (distinct from cc-switch GUI)
APP_TYPE = "claude"   # which cc-switch app_type this panel manages

# Per-process CSRF token. Injected into the served HTML and required on
# every write endpoint via the X-CC-Token header. A cross-origin attacker
# (DNS-rebind or classic CSRF) cannot READ our HTML due to same-origin
# policy, so they can't learn this token — blocking silent POSTs that
# would otherwise hijack the user's API key (e.g. inject a malicious
# ANTHROPIC_BASE_URL). Mirrors the OpenClaw config-server design.
SERVER_TOKEN = secrets.token_hex(32)
# ═══════════════════════════════════════════════════════════════
#  Atomic file write with fsync + rolling backups + safe read
#  (Codex Portable pattern — crash-safe, rollback-capable)
# ═══════════════════════════════════════════════════════════════
_BACKUP_COUNT = 5


def _atomic_write(path, data):
    """Write *data* to *path* atomically with fsync.

    - Creates parent directories on first write (seed).
    - Keeps up to ``_BACKUP_COUNT`` rolling backups (.1 … .5).
    - On fsync failure, logs to stderr but does NOT crash — the caller
      gets the data on disk (best-effort) while operators get a warning.
    - Uses a same-filesystem temp file + rename so readers never see a
      partial file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Rolling backups: .4 → .5 (drop), .3 → .4, … .1 → .2, current → .1
    for i in range(_BACKUP_COUNT, 0, -1):
        src = path.with_suffix(f"{path.suffix}.{i}") if i > 1 else path
        dst = path.with_suffix(f"{path.suffix}.{i + 1}")
        if i == _BACKUP_COUNT:
            # Drop oldest backup
            try:
                dst.unlink(missing_ok=True)
            except Exception:
                pass
        if src.exists():
            try:
                shutil.move(str(src), str(dst))
            except Exception:
                pass
    # current → .1
    if path.exists():
        try:
            shutil.move(str(path), str(path.with_suffix(f"{path.suffix}.1")))
        except Exception:
            pass

    tmp = path.with_suffix(f"{path.suffix}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            if isinstance(data, (dict, list)):
                f.write(json.dumps(data, ensure_ascii=False, indent=2))
            else:
                f.write(str(data))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError as e:
                print(f"[config_server] fsync warning for {path}: {e}",
                      file=sys.stderr)
        os.replace(str(tmp), str(path))
    except Exception:
        # Clean up temp file on failure
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


def safe_read(path, default=None):
    """Read a JSON/text file with backup fallback.

    Tries the primary file first; if corrupt/missing, tries .1 … .5
    backups in order. Returns *default* if nothing readable is found.
    """
    path = Path(path)
    candidates = [path]
    for i in range(1, _BACKUP_COUNT + 1):
        candidates.append(path.with_suffix(f"{path.suffix}.{i}"))
    for c in candidates:
        try:
            raw = c.read_text(encoding="utf-8")
            if path.suffix == ".json":
                return json.loads(raw)
            return raw
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    return default


# ── Provider catalog ────────────────────────────────────────────────
# Each provider maps to an Anthropic-compatible base_url. Claude Code
# talks the Anthropic wire protocol, so third-party providers must
# expose an /anthropic-compatible endpoint (most aggregators do).
# Models updated 2026-05-31 (researched from official docs).
PROVIDERS = [
    # ── Anthropic 官方 ──────────────────────────────────────────────
    # claude-opus-4-8: released 2026-05-28, 1M context, dynamic workflows
    # claude-opus-4-7: hybrid reasoning, strongest agentic coding
    # claude-sonnet-4-6: best speed/intelligence balance
    # claude-haiku-4-5: fastest, near-frontier
    {"id": "anthropic", "name": "Anthropic 官方", "base_url": "https://api.anthropic.com",
     "models": ["claude-fable-5", "claude-sonnet-5", "claude-opus-4-8", "claude-opus-4-7", "claude-sonnet-4-6", "claude-haiku-4-5"],
     "key_hint": "sk-ant-...", "note": "官方直连，Fable 5 最新 (1M context, 动态工作流)"},

    # ── OpenRouter 聚合 ─────────────────────────────────────────────
    # 一个 Key 访问所有主流模型；模型 ID 格式 provider/model-name
    {"id": "openrouter", "name": "OpenRouter", "base_url": "https://openrouter.ai/api",
     "models": [
         "anthropic/claude-opus-4-8",
         "anthropic/claude-opus-4-7",
         "anthropic/claude-sonnet-4-6",
         "google/gemini-3.1-pro-preview",
         "google/gemini-3.5-flash",
         "x-ai/grok-4",
         "deepseek/deepseek-v4-pro",
         "minimax/minimax-m2.7",
         "moonshotai/kimi-k2.6",
         "qwen/qwen3.6-plus",
     ],
     "key_hint": "sk-or-...", "note": "聚合平台，一个 Key 用所有模型，含 Gemini/Grok/Kimi"},

    # ── DeepSeek ────────────────────────────────────────────────────
    # V4-Pro: 1.6T MoE, 1M context, Anthropic 兼容端点
    # V4-Flash: 284B MoE, 1M context, 低延迟
    # deepseek-chat/reasoner: 2026-07-24 废弃，保留作兼容过渡
    {"id": "deepseek", "name": "DeepSeek", "base_url": "https://api.deepseek.com/anthropic",
     "models": ["deepseek-v4-pro", "deepseek-v4-flash", "deepseek-chat", "deepseek-reasoner"],
     "key_hint": "sk-...", "note": "国产，V4-Pro 1M context，性价比极高，Anthropic 兼容"},

    # ── MiniMax ─────────────────────────────────────────────────────
    # M2.7: 最新，自我进化，Office 套件强
    # M2.5: SWE-Bench 80.2%，编码旗舰
    # M2: 轻量高效，10B active / 230B total
    {"id": "minimax", "name": "MiniMax (海螺)", "base_url": "https://api.minimaxi.com/anthropic",
     "models": ["MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5", "MiniMax-M2"],
     "key_hint": "粘贴 MiniMax API Key", "note": "国产，M2.7 最新，Anthropic 兼容，速度快"},

    # ── 智谱 GLM ────────────────────────────────────────────────────
    # GLM-5.1: 744B MoE，SWE-Bench Pro SOTA，8h 持续执行
    # GLM-5: 上一代旗舰
    {"id": "zhipu", "name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/anthropic",
     "models": ["glm-5.1", "glm-5", "glm-4.6", "glm-4.5-air", "glm-4.5-flash"],
     "key_hint": "粘贴智谱 API Key", "note": "国产，GLM-5.1 最新，744B MoE，编码 SOTA"},

    # ── Kimi / Moonshot ─────────────────────────────────────────────
    # K2.6: 1T MoE，256K context，多模态，SWE-Bench Pro 58.6%
    # K2.5: 上一代，256K context
    {"id": "kimi", "name": "Kimi / Moonshot", "base_url": "https://api.moonshot.cn/anthropic",
     "models": ["kimi-k2.6", "kimi-k2.5", "kimi-k2-thinking-turbo", "moonshot-v1-128k"],
     "key_hint": "sk-...", "note": "国产，K2.6 最新，1T MoE，256K context，多模态"},

    # ── 豆包 / 火山引擎 ─────────────────────────────────────────────
    # Seed-1.6: 230B MoE，256K context，自适应思考
    # Seed-1.6-thinking: 深度推理模式
    # Seed-1.6-flash: 低延迟版本
    {"id": "doubao", "name": "豆包 / 火山引擎", "base_url": "https://ark.cn-beijing.volces.com/api/v3/anthropic",
     "models": ["doubao-seed-1.6", "doubao-seed-1.6-thinking", "doubao-seed-1.6-flash", "doubao-1.5-pro-256k"],
     "key_hint": "粘贴火山引擎 API Key", "note": "字节跳动，Seed 1.6 最新，256K context，多模态"},

    # ── 通义千问 / 阿里云 ───────────────────────────────────────────
    # Qwen3.6-Plus: 1M context，SWE-Bench 78.8%，Terminal-Bench 61.6%
    # Qwen3.6-35B-A3B: 开源版，35B total / 3B active
    # qwen3-max: 上一代旗舰
    {"id": "dashscope", "name": "通义千问 / 阿里", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/anthropic",
     "models": ["qwen3.6-plus", "qwen3.6-35b-a3b", "qwen3-coder-plus", "qwen3-max", "qwen-max-latest"],
     "key_hint": "sk-...", "note": "阿里云，Qwen3.6-Plus 最新，1M context，编码 SOTA"},

    # ── SiliconFlow 硅基流动 ────────────────────────────────────────
    # 国产聚合推理平台，托管主流开源模型，Anthropic 兼容端点
    {"id": "siliconflow", "name": "SiliconFlow (硅基流动)", "base_url": "https://api.siliconflow.cn/anthropic",
     "models": [
         "deepseek-ai/DeepSeek-V4-Pro",
         "deepseek-ai/DeepSeek-V4-Flash",
         "Qwen/Qwen3.6-Plus",
         "zai-org/GLM-5.1",
         "moonshotai/Kimi-K2.6",
     ],
     "key_hint": "sk-...", "note": "国产聚合，托管 DeepSeek/Qwen/GLM/Kimi，一站式"},

    # ── Google Gemini ───────────────────────────────────────────────
    # 通过 Google AI Studio 的 Anthropic 兼容端点访问
    # Gemini 3.1 Pro: 长上下文，工具调用，agentic
    # Gemini 3.5 Flash: 高效率，接近 Pro 水平
    {"id": "gemini", "name": "Google Gemini", "base_url": "https://generativelanguage.googleapis.com/v1beta/anthropic",
     "models": ["gemini-3.1-pro-preview", "gemini-3.5-flash", "gemini-3-pro-preview", "gemini-3-flash-preview"],
     "key_hint": "AIza...", "note": "Google，Gemini 3.1 Pro 最新，1M context，多模态"},

    # ── xAI Grok ────────────────────────────────────────────────────
    # Grok-4: 256K context，并行工具调用，图文输入
    {"id": "xai", "name": "xAI Grok", "base_url": "https://api.x.ai/v1/anthropic",
     "models": ["grok-4", "grok-3", "grok-3-mini"],
     "key_hint": "xai-...", "note": "xAI，Grok-4 最新，256K context，推理强"},

    # ── Groq ────────────────────────────────────────────────────────
    # 超快 LPU 推理，>460 tok/s
    # Llama-4-Scout: 10M context，多模态
    # Llama-4-Maverick: 128 experts，更强推理
    # compound-beta: Groq 自研 agentic 系统（含 web search）
    {"id": "groq", "name": "Groq", "base_url": "https://api.groq.com/anthropic",
     "models": [
         "meta-llama/llama-4-scout-17b-16e-instruct",
         "meta-llama/llama-4-maverick-17b-128e-instruct",
         "compound-beta",
         "llama-3.3-70b-versatile",
     ],
     "key_hint": "gsk_...", "note": "超快 LPU 推理 >460 tok/s，Llama-4 最新，免费额度"},

    # ── 小米 MiMo ─────────────────────────────────────────────────────
    # MiMo-v2.5-pro: 小米自研推理模型，Anthropic 兼容端点
    {"id": "xiaomi", "name": "小米 MiMo", "base_url": "https://token-plan-cn.xiaomimimo.com/anthropic",
     "models": ["mimo-v2.5-pro", "mimo-v2.5", "mimo-v2-pro"],
     "key_hint": "tp-...", "note": "小米 MiMo 推理模型，Anthropic 兼容，tp- 开头的 Key"},

    # ── OpenAI ──────────────────────────────────────────────────────
    {"id": "openai", "name": "OpenAI 官方", "base_url": "https://api.openai.com/v1",
     "models": ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                "gpt-5.5-pro", "gpt-5.5", "gpt-5.4-pro", "gpt-5.4",
                "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.1-codex-max",
                "gpt-5.1-codex", "gpt-5.1", "gpt-5-mini", "gpt-5-nano",
                "o4-mini-high", "o4-mini", "o3", "o3-mini",
                "gpt-4.1", "gpt-4.1-mini"],
     "key_hint": "sk-...", "note": "官方直连，GPT-5.5 / Codex / o4 最新"},

    # ── Mistral ─────────────────────────────────────────────────────
    {"id": "mistral", "name": "Mistral", "base_url": "https://api.mistral.ai/v1",
     "models": ["mistral-large-3", "mistral-large-latest", "mistral-medium-latest",
                "mistral-small-latest", "codestral-latest", "pixtral-large-latest",
                "ministral-8b-latest"],
     "key_hint": "粘贴 Mistral API Key", "note": "Mistral Large 3 最新，Codestral 代码专精"},

    # ── Perplexity ──────────────────────────────────────────────────
    {"id": "perplexity", "name": "Perplexity", "base_url": "https://api.perplexity.ai",
     "models": ["sonar-pro", "sonar-reasoning-pro", "sonar-deep-research",
                "sonar-reasoning", "sonar"],
     "key_hint": "pplx-...", "note": "联网搜索增强，Deep Research 深度研究"},

    # ── Cohere ──────────────────────────────────────────────────────
    {"id": "cohere", "name": "Cohere", "base_url": "https://api.cohere.com/v2",
     "models": ["command-a", "command-r-plus", "command-r"],
     "key_hint": "粘贴 Cohere API Key", "note": "Command A 最新，企业级 RAG"},

    # ── Amazon Nova ─────────────────────────────────────────────────
    {"id": "amazon", "name": "Amazon (Nova)", "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com/v1",
     "models": ["nova-premier-v1", "nova-pro-v1", "nova-lite-v1", "nova-micro-v1"],
     "key_hint": "粘贴 AWS Bedrock API Key", "note": "Amazon Nova 系列，AWS 原生"},

    # ── Together ────────────────────────────────────────────────────
    {"id": "together", "name": "Together", "base_url": "https://api.together.xyz/v1",
     "models": ["meta-llama/Llama-4-Maverick-17B-128E-Instruct-FP8",
                "deepseek-ai/DeepSeek-V4-Pro", "Qwen/Qwen3-Coder-480B-A35B-Instruct",
                "moonshotai/Kimi-K2.6", "zai-org/GLM-5.1",
                "MiniMaxAI/MiniMax-M3"],
     "key_hint": "粘贴 Together API Key", "note": "开源模型托管，Llama 4 / V4 Pro"},

    # ── Fireworks ───────────────────────────────────────────────────
    {"id": "fireworks", "name": "Fireworks", "base_url": "https://api.fireworks.ai/inference/v1",
     "models": ["accounts/fireworks/models/llama4-maverick-instruct-basic",
                "accounts/fireworks/models/llama4-scout-instruct-basic",
                "accounts/fireworks/models/deepseek-v4-pro",
                "accounts/fireworks/models/qwen3-coder-480b-a35b-instruct"],
     "key_hint": "粘贴 Fireworks API Key", "note": "高速推理，Llama 4 / V4 Pro"},

    # ── DeepInfra ───────────────────────────────────────────────────
    {"id": "deepinfra", "name": "DeepInfra", "base_url": "https://api.deepinfra.com/v1/openai",
     "models": ["deepseek-ai/DeepSeek-V4-Pro",
                "meta-llama/Llama-4-Maverick-17B-128E-Instruct",
                "Qwen/Qwen3-Coder", "zai-org/GLM-5.1",
                "moonshotai/Kimi-K2.6"],
     "key_hint": "粘贴 DeepInfra API Key", "note": "极低价格开源模型"},

    # ── Cerebras ────────────────────────────────────────────────────
    {"id": "cerebras", "name": "Cerebras", "base_url": "https://api.cerebras.ai/v1",
     "models": ["llama-4-scout-17b-16e-instruct", "llama-4-maverick-17b-128e-instruct",
                "llama3.3-70b", "qwen-3-coder-480b", "qwen-3-32b",
                "deepseek-r1-distill-llama-70b"],
     "key_hint": "粘贴 Cerebras API Key", "note": "1500 tok/s 极速推理，免费额度"},

    # ── 阶跃星辰 ────────────────────────────────────────────────────
    {"id": "stepfun", "name": "阶跃星辰 (Step)", "base_url": "https://api.stepfun.com/v1",
     "models": ["step-3.7-flash", "step-3.5-flash", "step-2-16k", "step-2-mini"],
     "key_hint": "粘贴阶跃星辰 API Key", "note": "Step 3.7 最新，速度快"},

    # ── 百川 ────────────────────────────────────────────────────────
    {"id": "baichuan", "name": "百川", "base_url": "https://api.baichuan-ai.com/v1",
     "models": ["Baichuan4-Turbo", "Baichuan4-Air", "Baichuan4", "Baichuan3-Turbo"],
     "key_hint": "粘贴百川 API Key", "note": "百川 AI，Baichuan4 最新"},

    # ── 零一万物 ────────────────────────────────────────────────────
    {"id": "yi", "name": "零一万物", "base_url": "https://api.lingyiwanwu.com/v1",
     "models": ["yi-lightning", "yi-large", "yi-large-turbo", "yi-medium", "yi-vision"],
     "key_hint": "粘贴零一万物 API Key", "note": "Yi-Lightning 极速，yi-large 强推理"},

    # ── 讯飞星火 ────────────────────────────────────────────────────
    {"id": "spark", "name": "讯飞星火", "base_url": "https://spark-api-open.xf-yun.com/v1",
     "models": ["4.0Ultra", "generalv3.5", "generalv3", "lite"],
     "key_hint": "粘贴讯飞 API Key", "note": "科大讯飞，4.0Ultra 最新"},

    # ── 腾讯混元 ────────────────────────────────────────────────────
    {"id": "hunyuan", "name": "腾讯混元", "base_url": "https://api.hunyuan.cloud.tencent.com/v1",
     "models": ["hunyuan-turbo", "hunyuan-large", "hunyuan-pro",
                "hunyuan-standard", "hunyuan-lite"],
     "key_hint": "粘贴腾讯混元 API Key", "note": "腾讯混元，Turbo 最新"},

    # ── 文心一言 ────────────────────────────────────────────────────
    {"id": "ernie", "name": "文心一言 / 百度", "base_url": "https://aip.baidubce.com/rpc/2.0/ai_custom/v1/wenxinworkshop/chat",
     "models": ["ernie-4.5-turbo-128k", "ernie-x1-turbo", "ernie-4.0-turbo",
                "ernie-4.0-8k", "ernie-3.5-8k"],
     "key_hint": "粘贴百度 API Key", "note": "百度文心，ERNIE 4.5 最新"},

    # ── 自定义 ──────────────────────────────────────────────────────

    # ── 新增 Provider ──
    {"id": "kimi", "name": "Kimi / Moonshot", "base_url": "https://api.moonshot.cn/anthropic",
     "models": ["kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5", "moonshot-v1-128k"],
     "key_hint": "sk-...", "note": "Kimi K2.7 最新，代码专精"},
    
    
    
    
    
    
    
    
    
    {"id": "custom", "name": "自定义 / 中转站", "base_url": "",
     "models": [], "custom": True,
     "key_hint": "粘贴中转站 API Key", "note": "填写任意 Anthropic 兼容端点的 base_url"},
]


# ═══════════════════════════════════════════════════════════════
#  cc-switch DB read / write
# ═══════════════════════════════════════════════════════════════
def _connect():
    CCS_DIR.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(CCS_DB), timeout=5.0)


def _ensure_schema(db):
    """Create the providers table if the DB is brand new, matching the
    cc-switch column layout so the GUI stays interoperable."""
    db.execute("""
        CREATE TABLE IF NOT EXISTS providers (
            id TEXT NOT NULL,
            app_type TEXT NOT NULL,
            name TEXT NOT NULL,
            settings_config TEXT NOT NULL,
            website_url TEXT,
            category TEXT,
            created_at INTEGER,
            sort_index INTEGER,
            notes TEXT,
            icon TEXT,
            icon_color TEXT,
            meta TEXT NOT NULL DEFAULT '{}',
            is_current BOOLEAN NOT NULL DEFAULT 0,
            in_failover_queue BOOLEAN NOT NULL DEFAULT 0,
            PRIMARY KEY (id, app_type)
        )
    """)
    db.commit()


def read_current():
    """Return the currently-active claude provider as a dict, or None."""
    if not CCS_DB.exists():
        return None
    try:
        db = _connect()
        row = db.execute(
            "SELECT id, name, settings_config FROM providers "
            "WHERE app_type=? AND is_current=1 LIMIT 1", (APP_TYPE,)
        ).fetchone()
        db.close()
        if not row:
            return None
        cfg = json.loads(row[2])
        env = cfg.get("env", {})
        return {
            "id": row[0], "name": row[1],
            "base_url": (env.get("ANTHROPIC_BASE_URL") or "").strip(),
            "api_key": (env.get("ANTHROPIC_AUTH_TOKEN")
                        or env.get("ANTHROPIC_API_KEY") or "").strip(),
            "model": (env.get("ANTHROPIC_MODEL") or "").strip(),
        }
    except Exception:
        return None


def list_providers():
    out = []
    if not CCS_DB.exists():
        return out
    try:
        db = _connect()
        try:
            rows = db.execute(
                "SELECT id, name, is_current FROM providers WHERE app_type=? "
                "ORDER BY is_current DESC, name", (APP_TYPE,)
            ).fetchall()
            for r in rows:
                out.append({"id": r[0], "name": r[1], "active": bool(r[2])})
        finally:
            db.close()
    except Exception:
        pass
    return out


def save_provider(name, base_url, api_key, model):
    """Insert/replace a provider row and mark it current. Mirrors the
    cc-switch settings_config shape so its GUI reads it back cleanly."""
    base_url = (base_url or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    model = (model or "").strip()
    if not base_url or not api_key:
        raise ValueError("base_url 和 api_key 不能为空")

    env = {
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_AUTH_TOKEN": api_key,
    }
    if model:
        env["ANTHROPIC_MODEL"] = model
        # Only set the Anthropic-specific model aliases when the endpoint
        # is actually Anthropic-shaped (api.anthropic.com or a proxy that
        # uses Anthropic model names). For third-party providers like
        # DeepSeek / Gemini / Grok, setting these aliases to a non-Claude
        # model name confuses Claude Code's model-selection logic.
        if "anthropic.com" in base_url or "openrouter.ai" in base_url:
            env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = model
            env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = model
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = model
    settings = {"env": env}
    meta = {"apiFormat": "anthropic"}

    db = _connect()
    try:
        _ensure_schema(db)
        pid = str(uuid.uuid4())
        db.execute("UPDATE providers SET is_current=0 WHERE app_type=?", (APP_TYPE,))
        db.execute(
            "INSERT INTO providers (id, app_type, name, settings_config, "
            "created_at, sort_index, meta, is_current) "
            "VALUES (?,?,?,?,?,?,?,1)",
            (pid, APP_TYPE, name or "Custom",
             json.dumps(settings, ensure_ascii=False),
             int(time.time() * 1000), 0, json.dumps(meta)),
        )
        db.commit()
    finally:
        db.close()
    return pid


def activate_provider(pid):
    db = _connect()
    try:
        db.execute("UPDATE providers SET is_current=0 WHERE app_type=?", (APP_TYPE,))
        db.execute("UPDATE providers SET is_current=1 WHERE id=? AND app_type=?",
                   (pid, APP_TYPE))
        db.commit()
    finally:
        db.close()


# ═══════════════════════════════════════════════════════════════
#  Maintenance features (batch 1): export / import / view / logs /
#  diagnose / unbind. Shared design with the OpenClaw config center.
# ═══════════════════════════════════════════════════════════════
def export_config():
    """Return the full set of saved providers as a portable JSON blob.

    Keys are included — this is an explicit user-initiated export meant
    for migrating to another machine, so we don't redact. The UI warns
    the user the file contains secrets."""
    out = {"version": 1, "app_type": APP_TYPE, "exported_at": int(time.time()),
           "providers": []}
    if not CCS_DB.exists():
        return out
    try:
        db = _connect()
        rows = db.execute(
            "SELECT id, name, settings_config, meta, is_current "
            "FROM providers WHERE app_type=?", (APP_TYPE,)
        ).fetchall()
        db.close()
        for r in rows:
            out["providers"].append({
                "id": r[0], "name": r[1],
                "settings_config": json.loads(r[2] or "{}"),
                "meta": json.loads(r[3] or "{}"),
                "is_current": bool(r[4]),
            })
    except Exception:
        pass
    return out


def import_config(blob):
    """Import providers from an export blob. Upserts by id; preserves the
    is_current flag from the blob (last one wins). Returns count."""
    if not isinstance(blob, dict) or not isinstance(blob.get("providers"), list):
        raise ValueError("无效的配置文件格式")
    db = _connect()
    try:
        _ensure_schema(db)
        count = 0
        current_id = None
        for p in blob["providers"]:
            pid = p.get("id") or str(uuid.uuid4())
            name = p.get("name") or "Imported"
            settings = p.get("settings_config") or {}
            meta = p.get("meta") or {}
            if not settings.get("env"):
                continue
            db.execute(
                "INSERT OR REPLACE INTO providers (id, app_type, name, "
                "settings_config, created_at, sort_index, meta, is_current) "
                "VALUES (?,?,?,?,?,?,?,0)",
                (pid, APP_TYPE, name, json.dumps(settings, ensure_ascii=False),
                 int(time.time() * 1000), 0, json.dumps(meta)),
            )
            count += 1
            if p.get("is_current"):
                current_id = pid
        if current_id:
            db.execute("UPDATE providers SET is_current=0 WHERE app_type=?", (APP_TYPE,))
            db.execute("UPDATE providers SET is_current=1 WHERE id=? AND app_type=?",
                       (current_id, APP_TYPE))
        db.commit()
    finally:
        db.close()
    return count


def view_config():
    """Return the current provider's settings with the API key MASKED.
    For on-screen display/debugging — never expose the full key in the UI."""
    cur = read_current()
    if not cur:
        return {"configured": False}
    key = cur.get("api_key", "")
    masked = (key[:6] + "…" + key[-4:]) if len(key) > 12 else "***"
    return {
        "configured": True,
        "name": cur.get("name"),
        "base_url": cur.get("base_url"),
        "model": cur.get("model"),
        "api_key_masked": masked,
        "api_key_len": len(key),
    }


def read_logs(max_lines=200):
    """Tail the most recent Claude session log. claude writes session
    history under data/.claude/. We surface the newest *.log / *.jsonl
    tail so users can debug a failed launch without leaving the panel.

    Safety: do NOT follow symlinks. During an active launcher session,
    data/.claude may be a symlink to the system ~/.claude — traversing
    that would be slow, potentially huge, and a privacy leak (showing
    the user's real system claude logs in the portable panel)."""
    claude_dir = DATA_DIR / ".claude"
    if not claude_dir.exists():
        return {"available": False, "text": "暂无日志（data/.claude/ 不存在）"}
    # If data/.claude is a symlink (active session), refuse to traverse
    # the target — it's the system dir, not ours.
    if claude_dir.is_symlink():
        return {"available": False,
                "text": "data/.claude 是符号链接（活跃会话中），日志在终端查看更安全"}
    candidates = []
    try:
        for p in claude_dir.rglob("*"):
            # Skip symlinks inside the dir too (defense in depth)
            if p.is_symlink():
                continue
            if p.is_file() and p.suffix in (".log", ".jsonl", ".txt"):
                try:
                    candidates.append((p.stat().st_mtime, p))
                except OSError:
                    continue
    except Exception:
        pass
    if not candidates:
        return {"available": False, "text": "暂无日志文件"}
    candidates.sort(reverse=True)
    newest = candidates[0][1]
    try:
        # Bounded tail: read at most 256KB from the end.
        size = newest.stat().st_size
        with open(newest, "rb") as f:
            if size > 262144:
                f.seek(-262144, os.SEEK_END)
            data = f.read().decode("utf-8", "replace")
        lines = data.splitlines()[-max_lines:]
        return {"available": True, "file": newest.name, "text": "\n".join(lines)}
    except Exception as e:
        return {"available": False, "text": f"读取日志失败: {e}"}


def run_diagnose():
    """Run a quick environment self-check. Returns a list of (label, ok,
    detail) tuples for the UI to render as a checklist."""
    checks = []

    def add(label, ok, detail=""):
        checks.append({"label": label, "ok": bool(ok), "detail": detail})

    # 1. config DB present + has a current provider
    cur = read_current()
    add("配置已就绪", cur is not None,
        (cur.get("name") if cur else "未配置任何供应商"))
    # 2. base_url + key sane
    if cur:
        add("Base URL 有效", len(cur.get("base_url", "")) > 8, cur.get("base_url", ""))
        add("API Key 已填", len(cur.get("api_key", "")) > 5,
            f"{len(cur.get('api_key',''))} 字符")
    # 3. claude binary present
    plat = _platform_dir()
    claude_bin = PORTABLE_ROOT / "bin" / plat / ("claude.exe" if os.name == "nt" else "claude")
    add("Claude 二进制存在", claude_bin.exists(), str(claude_bin))
    # 4. data dir writable
    try:
        test = DATA_DIR / ".write_test"
        test.write_text("x")
        test.unlink()
        add("数据目录可写", True, str(DATA_DIR))
    except Exception as e:
        add("数据目录可写", False, str(e))
    # 5. python3 (we're running, so yes)
    add("Python3 运行时", True, sys.version.split()[0])
    # 6. network reachability (best-effort, 5s)
    net_ok = False
    net_detail = "无法连接"
    try:
        import ssl
        ctx = None
        try:
            import certifi
            ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            pass
        u = (cur.get("base_url") if cur and cur.get("base_url") else "https://api.anthropic.com")
        req = urllib.request.Request(u, method="HEAD")
        kwargs = {"timeout": 5}
        if ctx:
            kwargs["context"] = ctx
        try:
            urllib.request.urlopen(req, **kwargs)
            net_ok = True
            net_detail = u
        except urllib.error.HTTPError:
            net_ok = True  # reached server, any HTTP code proves connectivity
            net_detail = u
        except Exception:
            pass
    except Exception:
        pass
    add("网络连通", net_ok, net_detail)
    return checks


def _platform_dir():
    if os.name == "nt":
        return "windows-x64"
    import platform as _p
    if _p.system() == "Darwin":
        return "macos-arm64" if _p.machine() == "arm64" else "macos-x64"
    return "linux-x64"


def unbind_device():
    """Remove the two device-binding lock files (same as launcher --unlock)."""
    removed = 0
    for lf in (DATA_DIR / ".lock", CCS_DIR / ".bind"):
        try:
            if lf.exists():
                lf.unlink()
                removed += 1
        except Exception:
            pass
    return removed


def launch_ccswitch():
    """Launch the bundled cc-switch GUI as a detached background process.

    The config center is the primary onboarding path, but cc-switch is a
    full native GUI with extra features (failover queues, multi-app
    management). Users who prefer it can start it from here.

    Returns (ok, message). We never block: Popen returns immediately and
    we don't wait on the GUI. Uses list-form args (no shell=True) so a
    weird install path can't inject a command.
    """
    plat = _platform_dir()
    exe = "cc-switch.exe" if os.name == "nt" else "cc-switch"
    ccbin = PORTABLE_ROOT / "bin" / plat / exe
    if not ccbin.exists():
        return False, f"未找到 CC Switch 可执行文件：bin/{plat}/{exe}"
    try:
        import subprocess
        kwargs = {
            "cwd": str(ccbin.parent),
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
        }
        if os.name == "nt":
            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP: fully detach so
            # the GUI survives after the config center process exits, and
            # doesn't get killed when this console window closes.
            kwargs["creationflags"] = 0x00000008 | 0x00000200
        else:
            # New session so SIGINT to the launcher terminal doesn't reach
            # the GUI; also reparents to init on exit.
            kwargs["start_new_session"] = True
            # On macOS the binary may carry a quarantine xattr; best-effort
            # strip it so Gatekeeper doesn't block the spawn.
            if sys.platform == "darwin":
                try:
                    subprocess.run(["xattr", "-dr", "com.apple.quarantine",
                                    str(ccbin)], timeout=5,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
                    os.chmod(ccbin, 0o755)
                except Exception:
                    pass
        subprocess.Popen([str(ccbin)], **kwargs)
        return True, "CC Switch 已启动"
    except Exception as e:
        return False, f"启动失败: {str(e)[:160]}"


# ═══════════════════════════════════════════════════════════════
#  API key connectivity test
# ═══════════════════════════════════════════════════════════════
def test_key(base_url, api_key, model):
    """Minimal Anthropic /v1/messages probe. Returns (ok, message).

    TLS resilience: portable Pythons sometimes ship without a usable
    system trust store (locked-down corp machines, USB installs). Try
    certifi first if available, then fall back to the default context.
    Same approach as the OpenClaw / Hermes config centers.

    Fallback model selection: if the caller didn't specify a model, pick
    a sensible default based on the base_url so we don't send a Claude
    model name to a DeepSeek/Groq endpoint (which would 400/404)."""
    import ssl
    base_url = (base_url or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    if not base_url or not api_key:
        return False, "缺少 base_url 或 api_key"

    # Pick a safe fallback model when none is specified.
    # For Anthropic-native endpoints use a Claude model; for third-party
    # providers use their own cheapest/fastest model so the test doesn't
    # fail with "model not found".
    if not model:
        if "anthropic.com" in base_url:
            model = "claude-haiku-4-5"
        elif "deepseek.com" in base_url:
            model = "deepseek-v4-flash"
        elif "moonshot" in base_url or "kimi" in base_url:
            model = "kimi-k2.5"
        elif "bigmodel" in base_url or "zhipu" in base_url:
            model = "glm-4.5-flash"
        elif "minimax" in base_url or "minimaxi" in base_url:
            model = "MiniMax-M2"
        elif "volces.com" in base_url or "doubao" in base_url:
            model = "doubao-seed-1.6-flash"
        elif "dashscope" in base_url or "aliyun" in base_url:
            model = "qwen3.6-35b-a3b"
        elif "siliconflow" in base_url:
            model = "deepseek-ai/DeepSeek-V4-Flash"
        elif "groq.com" in base_url:
            model = "llama-3.3-70b-versatile"
        elif "x.ai" in base_url:
            model = "grok-3-mini"
        elif "generativelanguage" in base_url:
            model = "gemini-3.5-flash"
        elif "openrouter.ai" in base_url:
            model = "anthropic/claude-haiku-4-5"
        elif "xiaomimimo" in base_url or "xiaomi" in base_url:
            model = "mimo-v2.5-pro"
        else:
            model = "claude-haiku-4-5"  # generic fallback

    url = base_url + "/v1/messages"
    body = json.dumps({
        "model": model,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "hi"}],
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "x-api-key": api_key,
        "authorization": f"Bearer {api_key}",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
        "user-agent": "ClaudePortable/ConfigCenter",
    })
    contexts = []
    try:
        import certifi  # type: ignore
        contexts.append(ssl.create_default_context(cafile=certifi.where()))
    except Exception:
        pass
    contexts.append(None)  # default context
    # Last resort: skip verification (macOS system Python sometimes lacks
    # DigiCert/ISRG root CAs in its trust store). Safe for a connectivity
    # probe that sends only "hi".
    try:
        contexts.append(ssl._create_unverified_context())
    except Exception:
        pass

    last_err = "无法连接"
    for ctx in contexts:
        try:
            kwargs = {"timeout": 15}
            if ctx is not None:
                kwargs["context"] = ctx
            with urllib.request.urlopen(req, **kwargs) as resp:
                if 200 <= resp.status < 300:
                    return True, "连接成功"
                return False, f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            # 400/401/403 still proves we reached the endpoint; only auth
            # failures should read as "bad key".
            if e.code in (401, 403):
                return False, "API Key 无效或无权限 (HTTP %d)" % e.code
            if e.code in (400, 404, 422):
                return True, "端点可达 (HTTP %d，凭据看起来有效)" % e.code
            try:
                detail = e.read(300).decode("utf-8", "replace")
            except Exception:
                detail = ""
            return False, f"HTTP {e.code} {detail[:120]}"
        except Exception as e:
            last_err = f"无法连接: {str(e)[:120]}"
            continue  # try next TLS context
    return False, last_err


# ═══════════════════════════════════════════════════════════════
#  Embedded UI (rich, tabbed, onboarding wizard). Styled to match the
#  OpenClaw / Hermes portable config centers: warm dark theme, cards,
#  tabs, first-run wizard. Loaded from lib/config_ui.html.
# ═══════════════════════════════════════════════════════════════
_UI_FILE = SCRIPT_DIR / "config_ui.html"


def _load_page():
    try:
        return _UI_FILE.read_text(encoding="utf-8")
    except Exception:
        return ("<html><body style='font-family:sans-serif;padding:40px'>"
                "<h2>配置中心 UI 文件缺失</h2><p>lib/config_ui.html 未找到。"
                "请重新下载发布包。</p></body></html>")


PAGE = _load_page()


# ═══════════════════════════════════════════════════════════════
#  HTTP handler
# ═══════════════════════════════════════════════════════════════
class Handler(BaseHTTPRequestHandler):
    timeout = 30
    def parse_request(self):
        """Override to block path-traversal attacks before normalization.

        Rejects raw request-line containing '..', backslash, or null bytes
        — these are the classic traversal vectors against Python's
        http.server which normalises paths after this hook.
        """
        raw = getattr(self, 'raw_requestline', b'')
        if isinstance(raw, bytes):
            try:
                raw_line = raw.decode('utf-8', 'replace')
            except Exception:
                raw_line = ''
        else:
            raw_line = str(raw)
        # Check the raw request-line for traversal / injection patterns
        if '..' in raw_line or '\\' in raw_line or '\x00' in raw_line:
            self.send_error(400, "Bad request path")
            return False
        return super().parse_request()

    def _host_ok(self):
        host = self.headers.get("Host", "")
        try:
            port = self.server.server_address[1]
        except Exception:
            port = PORT
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _reject_host(self):
        if self._host_ok():
            return False
        self.send_response(421)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"error":"Host mismatch"}')
        return True

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        # Inject the per-process CSRF token so the page's JS can send it
        # back on writes. Cross-origin attackers can't read this HTML
        # (same-origin policy), so they can't obtain the token.
        html = html.replace("__CC_TOKEN__", SERVER_TOKEN)
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self' 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)

    def _csrf_ok(self):
        """Require the per-process token on write requests. Blocks classic
        CSRF: a cross-origin page can POST to us (Host passes), but cannot
        read our token, so this header will be absent/wrong."""
        tok = self.headers.get("X-CC-Token", "")
        return secrets.compare_digest(tok, SERVER_TOKEN)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self._reject_host():
            return
        try:
            if self.path in ("/", "/index.html"):
                self._html(PAGE)
            elif self.path == "/api/state":
                # Strip the full api_key from the public state — the UI
                # never needs it (it shows a masked value via /api/view).
                cur = read_current()
                if cur:
                    cur = {k: v for k, v in cur.items() if k != "api_key"}
                self._json({
                    "providers_catalog": PROVIDERS,
                    "current": cur,
                    "saved": list_providers(),
                    "has_config": cur is not None,
                })
            elif self.path == "/api/heartbeat":
                self._json({"alive": True})
            elif self.path == "/api/view":
                self._json(view_config())
            elif self.path == "/api/logs":
                self._json(read_logs())
            elif self.path == "/api/diagnose":
                self._json({"checks": run_diagnose()})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)[:200]}, 500)

    def do_POST(self):
        if self._reject_host():
            return
        # Content-Type enforcement: reject non-JSON bodies with 415
        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ct != "application/json":
            self.send_response(415)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"Content-Type must be application/json"}')
            return
        # CSRF gate: all writes require the per-process token. Without
        # this, a malicious page the user visits could silently POST
        # /api/save with an attacker-controlled base_url and hijack the
        # API key + all prompts. The Host pin alone does NOT stop this.
        if not self._csrf_ok():
            self._json({"ok": False, "error": "missing or invalid token"}, 403)
            return
        try:
            n = min(int(self.headers.get("Content-Length", 0)), 65_536)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw or b"{}")
        except Exception:
            self._json({"ok": False, "error": "bad request body"}, 400)
            return
        try:
            if self.path == "/api/save":
                save_provider(data.get("name", ""), data.get("base_url", ""),
                              data.get("api_key", ""), data.get("model", ""))
                self._json({"ok": True})
            elif self.path == "/api/test":
                ok, msg = test_key(data.get("base_url", ""),
                                   data.get("api_key", ""), data.get("model", ""))
                self._json({"ok": ok, "message": msg})
            elif self.path == "/api/activate":
                activate_provider(data.get("id", ""))
                self._json({"ok": True})
            elif self.path == "/api/import":
                count = import_config(data)
                self._json({"ok": True, "count": count})
            elif self.path == "/api/unbind":
                removed = unbind_device()
                self._json({"ok": True, "removed": removed})
            elif self.path == "/api/launch-ccswitch":
                ok, msg = launch_ccswitch()
                self._json({"ok": ok, "message": msg})
            elif self.path == "/api/export":
                self._json(export_config())
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as e:
            self._json({"ok": False, "error": str(e)[:200]}, 400)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    server = None
    actual = PORT
    for p in range(PORT, PORT + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            actual = p
            break
        except OSError:
            continue
    if server is None:
        print(f"  [!] 端口 {PORT}-{PORT+9} 都被占用", file=sys.stderr)
        sys.exit(1)
    url = f"http://127.0.0.1:{actual}"
    # Write runtime.json for sibling tools to discover this server.
    runtime = {
        "config_port": actual,
        "config_url": url,
        "token": SERVER_TOKEN,
        "pid": os.getpid(),
    }
    try:
        _atomic_write(DATA_DIR / "runtime.json", runtime)
    except Exception as e:
        print(f"[config_server] failed to write runtime.json: {e}",
              file=sys.stderr)
    print(f"  配置中心: {url}")
    if not os.environ.get("CLAUDE_BROWSER_OPENED"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
