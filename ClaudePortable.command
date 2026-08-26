#!/bin/bash
set -u
# ═══════════════════════════════════════════
# Claude Code Portable + CC Switch · macOS
# ═══════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_TYPE='claude'
CONFIG_PORT=17580

# Clean up stale port
cleanup_stale_ports() {
  local port=$1
  local pid
  pid=$(lsof -ti :"$port" 2>/dev/null)
  if [ -n "$pid" ]; then
    echo "  [cleanup] Killing stale process on port $port (PID: $pid)"
    kill -9 "$pid" 2>/dev/null
    sleep 1
  fi
}
ARCH="$(uname -m)"

# ── resolve_python3 ──────────────────────────────────────
# Prefer bundled python3 (portable, no system dependency),
# fall back to system python3.
resolve_python3() {
    local bundled=""
    case "$ARCH" in
        arm64)  bundled="$SCRIPT_DIR/bin/macos-arm64/python3" ;;
        x86_64) bundled="$SCRIPT_DIR/bin/macos-x64/python3" ;;
    esac
    if [ -n "$bundled" ] && [ -x "$bundled" ]; then
        PYTHON3="$bundled"
        return 0
    fi
    if command -v python3 &>/dev/null; then
        PYTHON3="$(command -v python3)"
        return 0
    fi
    PYTHON3=""
    return 1
}
resolve_python3

# ── Preflight checks ─────────────────────────────────────
preflight() {
    local errors=0
    # Check claude binary exists (done again later, but fail early)
    local bin_sub
    case "$ARCH" in
        arm64)  bin_sub="macos-arm64" ;;
        x86_64) bin_sub="macos-x64" ;;
    esac
    local bin_dir="$SCRIPT_DIR/bin/$bin_sub"
    if [ ! -f "$bin_dir/claude" ]; then
        echo "  [preflight] FAIL: claude binary not found at $bin_dir/claude"
        errors=$((errors + 1))
    fi
    # Check config_server.py exists
    if [ ! -f "$SCRIPT_DIR/lib/config_server.py" ]; then
        echo "  [preflight] WARN: config_server.py not found (config UI unavailable)"
    fi
    # Check data dir is writable
    mkdir -p "$SCRIPT_DIR/data" 2>/dev/null
    if [ ! -w "$SCRIPT_DIR/data" ]; then
        echo "  [preflight] FAIL: data directory is not writable: $SCRIPT_DIR/data"
        errors=$((errors + 1))
    fi
    # Ensure logs dir exists
    mkdir -p "$SCRIPT_DIR/data/logs" 2>/dev/null
    if [ $errors -gt 0 ]; then
        echo "  [preflight] $errors error(s) detected. Fix issues above and retry."
        exit 1
    fi
}
# 处理 --unlock 参数（删除两个 lock 文件）
if [ "${1:-}" = "--unlock" ]; then
    LOCK_FILE="$SCRIPT_DIR/data/.lock"
    LOCK_FILE2="$SCRIPT_DIR/data/.cc-switch/.bind"
    REMOVED=0
    [ -f "$LOCK_FILE" ] && { rm -f "$LOCK_FILE"; REMOVED=1; }
    [ -f "$LOCK_FILE2" ] && { rm -f "$LOCK_FILE2"; REMOVED=1; }
    if [ "$REMOVED" = "1" ]; then
        echo "  [ok] 已移除绑定锁，下次运行将重新绑定到当前位置。"
    else
        echo "  [info] 没有绑定锁需要移除。"
    fi
    exit 0
fi

# macOS: 移除 quarantine 属性（Gatekeeper）— 必须在 preflight 之前，
# 否则首次运行时 --version 测试因隔离属性跑不起来，误报 "Binary won't run"
xattr -dr com.apple.quarantine "$BIN_DIR/claude" 2>/dev/null
xattr -dr com.apple.quarantine "$BIN_DIR/cc-switch" 2>/dev/null

preflight

# 处理 --config 参数（随时打开配置中心，不启动 Claude）
if [ "${1:-}" = "--config" ]; then
    CONFIG_SERVER="$SCRIPT_DIR/lib/config_server.py"
    if [ -n "${PYTHON3:-}" ] && [ -x "${PYTHON3:-}" ] && [ -f "$CONFIG_SERVER" ]; then
        echo "  打开配置中心 http://127.0.0.1:17580 ..."
        exec "$PYTHON3" "$CONFIG_SERVER"
    elif [ -x "$SCRIPT_DIR/bin/macos-arm64/cc-switch" ] || [ -x "$SCRIPT_DIR/bin/macos-x64/cc-switch" ]; then
        ARCH_CC="$(uname -m)"; CCBIN="$SCRIPT_DIR/bin/macos-x64/cc-switch"
        [ "$ARCH_CC" = "arm64" ] && CCBIN="$SCRIPT_DIR/bin/macos-arm64/cc-switch"
        exec "$CCBIN"
    else
        echo "  [!] 未找到 python3 或 cc-switch"
        exit 1
    fi
fi

# Banner
GOLD='\033[38;5;220m'
AMBER='\033[38;5;214m'
BRONZE='\033[38;5;166m'
NC='\033[0m'
echo ""
echo -e "${GOLD}  ██╗   ██╗██╗  ██╗   ██╗ ██████╗${NC}"
echo -e "${GOLD}  ╚██╗ ██╔╝██║  ╚██╗ ██╔╝██╔════╝${NC}"
echo -e "${AMBER}   ╚████╔╝ ██║   ╚████╔╝ ██║  ███╗${NC}"
echo -e "${AMBER}    ╚██╔╝  ██║    ╚██╔╝  ██║   ██║${NC}"
echo -e "${BRONZE}     ██║   ███████╗██║   ╚██████╔╝${NC}"
echo -e "${BRONZE}     ╚═╝   ╚══════╝╚═╝    ╚═════╝${NC}"
echo ""
echo "     Claude Code Portable"
echo ""

# 架构检测
case "$ARCH" in
    arm64)  BIN_DIR="$SCRIPT_DIR/bin/macos-arm64" ;;
    x86_64) BIN_DIR="$SCRIPT_DIR/bin/macos-x64" ;;
    *)      echo "[ERROR] 不支持的架构: $ARCH"; exit 1 ;;
esac

if [ ! -f "$BIN_DIR/claude" ]; then
    echo "[ERROR] 未找到 Claude Code: $BIN_DIR/claude"
    exit 1
fi

chmod +x "$BIN_DIR/claude" "$BIN_DIR/cc-switch" 2>/dev/null

# ═══════════════════════════════════════════
# 单实例锁（防止并发运行）
# 使用 mkdir 而非 touch — mkdir 是原子的，跨平台都靠谱。
# 双击 .command 触发两次终端启动时（macOS 常见），
# 之前的 [-f] 检查 + echo PID 是非原子的：两个实例可同时通过检查、
# 同时写入、互相覆盖 PID、互相清理 symlink。
# ═══════════════════════════════════════════
RUN_LOCK_DIR="$SCRIPT_DIR/data/.running"
mkdir -p "$SCRIPT_DIR/data"

# stale-lock 检测：如果存在但里面 PID 已死，清理后重试一次
if [ -d "$RUN_LOCK_DIR" ]; then
    PREV_PID=""
    [ -f "$RUN_LOCK_DIR/pid" ] && PREV_PID=$(cat "$RUN_LOCK_DIR/pid" 2>/dev/null | tr -d '[:space:]')
    if [ -n "$PREV_PID" ] && kill -0 "$PREV_PID" 2>/dev/null; then
        echo "  [info] 已有另一个实例正在运行 (PID $PREV_PID)。"
        echo "  如果错误，请删除：$RUN_LOCK_DIR"
        exit 1
    fi
    # Stale — 先尝试清理
    rm -rf "$RUN_LOCK_DIR" 2>/dev/null
fi

# mkdir 原子操作：成功 = 我们持锁；失败 = 别人正好同时建了。
if ! mkdir "$RUN_LOCK_DIR" 2>/dev/null; then
    echo "  [info] 已有另一个实例正在运行 (并发启动)。"
    echo "  如果错误，请删除：$RUN_LOCK_DIR"
    exit 1
fi
echo $$ > "$RUN_LOCK_DIR/pid"

# 兼容旧 RUN_LOCK 变量名（cleanup 引用）
RUN_LOCK="$RUN_LOCK_DIR"

# ═══════════════════════════════════════════
# 便携目录设置
# ═══════════════════════════════════════════
PORTABLE_DATA="$SCRIPT_DIR/data"
PORTABLE_CCS="$PORTABLE_DATA/.cc-switch"
PORTABLE_CLAUDE="$PORTABLE_DATA/.claude"
SYS_CCS="$HOME/.cc-switch"
SYS_CLAUDE="$HOME/.claude"
CCS_DB="$PORTABLE_CCS/cc-switch.db"
LIB_DIR="$SCRIPT_DIR/lib"
LOCK_FILE="$PORTABLE_DATA/.lock"

mkdir -p "$PORTABLE_CCS" "$PORTABLE_CLAUDE"

# ═══════════════════════════════════════════
# 设备绑定校验（双 lock 文件，删除一个不能绕过）
# ═══════════════════════════════════════════
LOCK_FILE2="$PORTABLE_CCS/.bind"
LOCK_PRESENT=0
[ -f "$LOCK_FILE" ] && LOCK_PRESENT=1
[ -f "$LOCK_FILE2" ] && LOCK_PRESENT=1

if [ "$LOCK_PRESENT" = "1" ] && [ -f "$LIB_DIR/binding.sh" ]; then
    chmod +x "$LIB_DIR/binding.sh" 2>/dev/null
    # 校验任一存在的 lock。两个文件内容应一致——若不一致说明
    # 有人篡改了其中一个，按最严格的策略对待：用任一现存 lock
    # 检查；若其中一个 hash mismatch 但另一个 match，仍然 deny。
    # （但其实若 hash mismatch，说明此设备非原设备，两个 lock
    # 都会 mismatch，不存在「一对一错」的情况——除非用户故意改）
    bind_failed=0
    bind_warned=0
    for active_lock in "$LOCK_FILE" "$LOCK_FILE2"; do
        [ -f "$active_lock" ] || continue
        bash "$LIB_DIR/binding.sh" check "$SCRIPT_DIR" "$active_lock"
        r=$?
        case $r in
            1) bind_failed=1; break ;;   # mismatch → 立即拒绝
            3) bind_warned=1 ;;           # 无法计算 fingerprint
        esac
    done
    if [ "$bind_failed" = "1" ]; then
        echo ""
        echo "  ============================================================"
        echo "  [ERROR] 此便携包已绑定到原始设备。"
        echo "  ============================================================"
        echo ""
        echo "  当前位置与绑定设备不匹配。这是防复制保护机制。"
        echo "  此便携包不能被复制到其他设备运行。"
        echo ""
        echo "  如果你是原始所有者并主动移动了它："
        echo "    ./ClaudePortable.command --unlock"
        echo ""
        exit 1
    fi
    if [ "$bind_warned" = "1" ]; then
        echo "  [warn] 无法验证设备绑定（继续运行）。"
    fi
fi

# 注意：之前这里有一个 `migrate_dir`，它先把 ~/.cc-switch 拷到便携包，
# 然后又调 `ensure_symlink`。结果是 ensure_symlink 看到便携包有数据、
# 系统目录也有数据 → 走 "backup system dir" 分支，把 ~/.cc-switch
# 重命名为 .before-portable.<timestamp>。这是噪声不是数据丢失，但用户
# 体验差。改为单路径：直接调 ensure_symlink，让它一气呵成处理迁移、
# 拷贝、链接。
ensure_symlink() {
    local link="$1" target="$2"
    # 已经是指向目标的符号链接 → 幂等
    if [ -L "$link" ]; then
        local current="$(readlink "$link")"
        if [ "$current" = "$target" ]; then
            return 0
        fi
        # 指向其他位置，删除并重建
        rm "$link" 2>/dev/null
    elif [ -d "$link" ]; then
        # 真目录 — 用户有预装的系统版本。
        # 仅当便携目录为空时才迁移（避免覆盖便携数据）。
        # 绝不 rm -rf 用户数据。
        if [ -n "$(ls -A "$link" 2>/dev/null)" ]; then
            if [ -z "$(ls -A "$target" 2>/dev/null)" ]; then
                echo "  [migrate] $link → $target"
                # 重要：cp 失败时绝不能删源数据。
                # 之前的版本是 `cp -a ... 2>/dev/null` 然后 rm -rf，
                # 如果 cp 因磁盘满/权限/文件锁部分失败，会永久丢失用户数据。
                local cp_err
                cp_err=$(mktemp -t claude-cp.XXXXXX 2>/dev/null) || cp_err="/tmp/claude-cp.$$"
                if cp -a "$link/." "$target/" 2>"$cp_err"; then
                    rm -f "$cp_err"
                else
                    echo "  [ERROR] 迁移失败，保留系统目录不删除：$link"
                    [ -s "$cp_err" ] && sed 's/^/    /' "$cp_err" >&2
                    rm -f "$cp_err"
                    # 系统目录还在，便携包没数据 → 直接退出，不创建符号链接
                    # 让用户手动决断比静默丢数据强
                    return 1
                fi
            else
                echo "  [warn] 便携包已有数据，跳过系统目录迁移: $link"
                # 把系统目录改名备份而不是删除
                local backup="${link}.before-portable.$(date +%Y%m%d-%H%M%S)"
                mv "$link" "$backup" 2>/dev/null && echo "  [info] 系统数据已备份到: $backup"
                ln -s "$target" "$link" 2>/dev/null
                return 0
            fi
        fi
        # 走到这里：cp 已成功（或源目录原本就是空的）。
        # 删除源目录腾出位置创建符号链接。
        rm -rf "$link" 2>/dev/null
    fi
    ln -s "$target" "$link" 2>/dev/null
}
ensure_symlink "$SYS_CCS" "$PORTABLE_CCS"
ensure_symlink "$SYS_CLAUDE" "$PORTABLE_CLAUDE"

CC_SWITCH_PID=""
WE_STARTED_CCS=0

# 退出时清理符号链接（只清符号链接，不动真目录）
cleanup() {
    # 优雅杀掉本脚本启动的 cc-switch
    if [ "$WE_STARTED_CCS" = "1" ] && [ -n "$CC_SWITCH_PID" ] && kill -0 "$CC_SWITCH_PID" 2>/dev/null; then
        # 重要：先收集子进程列表再 kill 父进程。
        # 父进程被 kill 后，子进程会 reparent 到 init (PID 1)，
        # `pgrep -P parent_pid` 就找不到它们了。
        local children
        children=$(pgrep -P "$CC_SWITCH_PID" 2>/dev/null || true)

        # 先 SIGTERM 主进程
        kill -TERM "$CC_SWITCH_PID" 2>/dev/null
        # 等最多 5 秒让 Electron 优雅关闭
        for _ in 1 2 3 4 5; do
            kill -0 "$CC_SWITCH_PID" 2>/dev/null || break
            sleep 1
        done
        # 还活着 → SIGKILL
        if kill -0 "$CC_SWITCH_PID" 2>/dev/null; then
            kill -9 "$CC_SWITCH_PID" 2>/dev/null
        fi
        # 清理之前收集的子进程（包括优雅关闭时漏掉的）
        for child in $children; do
            kill -9 "$child" 2>/dev/null
        done
    fi
    [ -L "$SYS_CCS" ] && rm "$SYS_CCS" 2>/dev/null
    [ -L "$SYS_CLAUDE" ] && rm "$SYS_CLAUDE" 2>/dev/null
    # 清理单实例锁（目录形式，原子持锁机制）
    [ -d "$RUN_LOCK" ] && rm -rf "$RUN_LOCK"
}
trap cleanup EXIT INT TERM

# ═══════════════════════════════════════════
# 检查配置（验证 DB 中真有 provider，不只是文件大小）
# ═══════════════════════════════════════════
has_valid_config() {
    [ -f "$CCS_DB" ] || return 1
    if command -v python3 &>/dev/null; then
        : # 走 python3 路径
    elif command -v sqlite3 &>/dev/null; then
        # python3 不在 → 退化到 sqlite3 CLI（macOS 自带 /usr/bin/sqlite3）
        local row
        row=$(sqlite3 -readonly "$CCS_DB" \
            "SELECT settings_config FROM providers WHERE app_type='claude' AND is_current=1 LIMIT 1;" 2>/dev/null)
        # 要求 key 后面跟非空字符串值（>=6 字符），否则空值会误判已配置
        if [ -n "$row" ] && \
           printf '%s' "$row" | grep -qE '"ANTHROPIC_BASE_URL"[[:space:]]*:[[:space:]]*"[^"]{6,}"' && \
           printf '%s' "$row" | grep -qE '"ANTHROPIC_(AUTH_TOKEN|API_KEY)"[[:space:]]*:[[:space:]]*"[^"]{6,}"'; then
            return 0
        fi
        return 1
    else
        # 既无 python3 也无 sqlite3。Size 检查不可靠：
        # 空 SQLite DB 文件经常 > 4096 字节（schema + page header 占空间）。
        # 此情况我们「返回未配置」（fail-closed），让 first-run 流程
        # 启动 GUI，比误判已配置然后用空 token 启动 Claude 更安全。
        return 1
    fi
    CCS_DB="$CCS_DB" python3 - <<'PYEOF' 2>/dev/null
import sqlite3, json, os, sys
# Use SQLite query instead of regex on raw bytes:
#   - regex would match deleted-but-not-vacuumed rows (SQLite lazy delete)
#   - it would also match across rows, returning false positive
#   - reading the whole file as text wastes memory on large DBs
try:
    db = sqlite3.connect(os.environ['CCS_DB'], timeout=2.0)
    row = db.execute(
        "SELECT settings_config FROM providers "
        "WHERE app_type='claude' AND is_current=1 LIMIT 1"
    ).fetchone()
    db.close()
    if not row:
        sys.exit(1)
    cfg = json.loads(row[0])
    env = cfg.get('env', {})
    url = env.get('ANTHROPIC_BASE_URL', '')
    key = env.get('ANTHROPIC_AUTH_TOKEN', '') or env.get('ANTHROPIC_API_KEY', '')
    if isinstance(url, str) and isinstance(key, str) and len(url) > 5 and len(key) > 5:
        sys.exit(0)
except Exception:
    pass
sys.exit(1)
PYEOF
}

if ! has_valid_config; then
    echo "═══════════════════════════════════════════"
    echo "  首次运行 - 配置 API"
    echo "═══════════════════════════════════════════"
    echo ""
    CONFIG_SERVER="$LIB_DIR/config_server.py"
    if command -v python3 &>/dev/null && [ -f "$CONFIG_SERVER" ]; then
        # 优先用配置中心（图文引导，浏览器里完成）
        echo "  正在打开配置中心 http://127.0.0.1:17580 ..."
        echo "  按引导选供应商、填 Key、测试、保存即可。"
        echo ""
        mkdir -p "$SCRIPT_DIR/data/logs" 2>/dev/null
        "${PYTHON3:-python3}" "$CONFIG_SERVER" >>"$SCRIPT_DIR/data/logs/config_server.log" 2>&1 &
        CC_SWITCH_PID=$!
        WE_STARTED_CCS=1
    elif [ -x "$BIN_DIR/cc-switch" ]; then
        # 回退：cc-switch GUI
        echo "  正在打开 CC Switch GUI..."
        echo "  添加一个 Provider 并保存（无需关闭 CC Switch）"
        echo ""
        "$BIN_DIR/cc-switch" >/dev/null 2>&1 &
        CC_SWITCH_PID=$!
        WE_STARTED_CCS=1
    else
        echo "  [!] 未找到 python3 或 cc-switch，无法配置。"
        echo "  请安装 python3 后重试，或手动编辑 $CCS_DB"
        exit 1
    fi

    echo "  等待配置..."
    # 5 分钟最长等待。每 30 秒打一个进度提示，让用户知道脚本还活着。
    # 同时检测 cc-switch 进程意外死亡 — 如果死了就立刻报错，
    # 而不是干等 5 分钟。
    for i in $(seq 1 150); do
        sleep 2
        if has_valid_config; then
            echo ""
            echo "  [ok] 检测到 Provider，继续启动"
            sleep 1
            break
        fi
        # cc-switch 进程已经退出？用户关掉了，或者 GUI 崩了。
        if ! kill -0 "$CC_SWITCH_PID" 2>/dev/null; then
            echo ""
            echo "  [!] 配置工具已退出但仍未检测到 Provider 配置。"
            echo "  请重新运行并完成配置。"
            exit 1
        fi
        # 每 15 次（30 秒）打一个点
        if [ $((i % 15)) -eq 0 ]; then
            printf "."
        fi
    done

    if ! has_valid_config; then
        echo "  [!] 等待超时，请重新运行"
        exit 1
    fi
else
    # 已有配置，仍然启动配置中心（后台非阻塞），方便随时修改 Key
    CONFIG_SERVER="$LIB_DIR/config_server.py"
    if [ -n "${PYTHON3:-}" ] && [ -f "$CONFIG_SERVER" ]; then
        echo "  配置中心已启动: http://127.0.0.1:17580"
        echo "  （如需修改 Key，在浏览器中操作后保存即可）"
        mkdir -p "$SCRIPT_DIR/data/logs" 2>/dev/null
        "$PYTHON3" "$CONFIG_SERVER" >>"$SCRIPT_DIR/data/logs/config_server.log" 2>&1 &
        CC_SWITCH_PID=$!
        WE_STARTED_CCS=1
    fi
fi

# ═══════════════════════════════════════════
# 从 DB 读取 API 配置
# ═══════════════════════════════════════════
read_config() {
    # python3 路径（优先，最准确）
    if command -v python3 &>/dev/null; then
        local result attempt
        for attempt in 1 2 3; do
            result=$(CCS_DB="$CCS_DB" python3 - <<'PYEOF' 2>/dev/null
import sqlite3, json, os, sys
try:
    # timeout=2.0: don't block more than 2s on a writer lock.
    # The outer bash loop retries 3 times so total worst-case is ~6s,
    # vs the SQLite default of 5s × 3 = 15s.
    db = sqlite3.connect(os.environ['CCS_DB'], timeout=2.0)
    row = db.execute("SELECT settings_config FROM providers WHERE app_type='claude' AND is_current=1 LIMIT 1").fetchone()
    db.close()
    if row:
        cfg = json.loads(row[0])
        env = cfg.get('env', {})
        bu = env.get('ANTHROPIC_BASE_URL', '')
        ak = env.get('ANTHROPIC_AUTH_TOKEN', '') or env.get('ANTHROPIC_API_KEY', '')
        md = env.get('ANTHROPIC_MODEL', '')
        # Trim whitespace — cc-switch may store keys with trailing whitespace
        # if user pasted with extra characters. Without this, claude API
        # calls would 404/401 on the malformed credential.
        if isinstance(bu, str): bu = bu.strip()
        if isinstance(ak, str): ak = ak.strip()
        if isinstance(md, str): md = md.strip()
        if bu and ak:
            print(bu)
            print(ak)
            # 3rd line: model (may be empty). Always print so the reader's
            # line indexing is stable.
            print(md)
except Exception:
    pass
PYEOF
)
            if [ -n "$result" ]; then
                # 用 printf 而不是 echo 避免反斜杠转义问题
                # （某些 echo 实现会解释 \n、\t 等）
                export ANTHROPIC_BASE_URL=$(printf '%s\n' "$result" | sed -n '1p')
                export ANTHROPIC_AUTH_TOKEN=$(printf '%s\n' "$result" | sed -n '2p')
                local _md=$(printf '%s\n' "$result" | sed -n '3p')
                if [ -n "$_md" ]; then
                    export ANTHROPIC_MODEL="$_md"
                else
                    unset ANTHROPIC_MODEL
                fi
                unset ANTHROPIC_API_KEY
                return 0
            fi
            sleep 1
        done
        return 1
    fi

    # sqlite3 CLI fallback (macOS 自带 /usr/bin/sqlite3)。
    # 不依赖 python3，但需要手工解析 JSON。
    if command -v sqlite3 &>/dev/null; then
        local row attempt
        for attempt in 1 2 3; do
            row=$(sqlite3 -readonly "$CCS_DB" \
                "SELECT settings_config FROM providers WHERE app_type='claude' AND is_current=1 LIMIT 1;" 2>/dev/null)
            if [ -n "$row" ]; then
                # 极简 JSON 解析：只抽 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN
                # （实际 row 是 cfg.settings_config，里面是 {"env":{...}}）
                local bu ak md
                bu=$(printf '%s' "$row" | sed -nE 's/.*"ANTHROPIC_BASE_URL"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' | head -1)
                ak=$(printf '%s' "$row" | sed -nE 's/.*"ANTHROPIC_AUTH_TOKEN"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' | head -1)
                [ -z "$ak" ] && ak=$(printf '%s' "$row" | sed -nE 's/.*"ANTHROPIC_API_KEY"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' | head -1)
                md=$(printf '%s' "$row" | sed -nE 's/.*"ANTHROPIC_MODEL"[[:space:]]*:[[:space:]]*"([^"]+)".*/\1/p' | head -1)
                # 去前后空白
                bu=$(printf '%s' "$bu" | awk '{$1=$1};1')
                ak=$(printf '%s' "$ak" | awk '{$1=$1};1')
                md=$(printf '%s' "$md" | awk '{$1=$1};1')
                if [ -n "$bu" ] && [ -n "$ak" ]; then
                    export ANTHROPIC_BASE_URL="$bu"
                    export ANTHROPIC_AUTH_TOKEN="$ak"
                    if [ -n "$md" ]; then
                        export ANTHROPIC_MODEL="$md"
                    else
                        unset ANTHROPIC_MODEL
                    fi
                    unset ANTHROPIC_API_KEY
                    return 0
                fi
            fi
            sleep 1
        done
        return 1
    fi

    echo "  [!] 需要 python3 或 sqlite3 读取配置"
    return 1
}

if read_config; then
    echo "  [ok] 配置已加载"
else
    echo "  [!] 无法从 DB 读取配置"
    exit 1
fi

# ═══════════════════════════════════════════
# 同步 env 到 .claude/settings.json
# Claude Code 优先读 settings.json 的 env，如果不同步，
# 旧的 settings.json 会覆盖启动器注入的环境变量。
# ═══════════════════════════════════════════
CLAUDE_SETTINGS="$PORTABLE_CLAUDE/settings.json"
sync_claude_settings() {
    command -v python3 &>/dev/null || return 0
    CCS_ENV_MODEL="${ANTHROPIC_MODEL:-}" \
    CCS_ENV_URL="$ANTHROPIC_BASE_URL" \
    CCS_ENV_KEY="$ANTHROPIC_AUTH_TOKEN" \
    CCS_SETTINGS_FILE="$CLAUDE_SETTINGS" \
    python3 - <<'PYEOF' 2>/dev/null
import json, os
sf = os.environ['CCS_SETTINGS_FILE']
try:
    with open(sf, 'r') as f:
        cfg = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    cfg = {}
env = cfg.get('env', {})
changed = False
for k, v in [
    ('ANTHROPIC_BASE_URL', os.environ['CCS_ENV_URL']),
    ('ANTHROPIC_AUTH_TOKEN', os.environ['CCS_ENV_KEY']),
    ('ANTHROPIC_MODEL', os.environ.get('CCS_ENV_MODEL', '')),
]:
    if v and env.get(k) != v:
        env[k] = v
        changed = True
# 清除可能残留的旧模型别名（如 MiniMax 的 DEFAULT_*_MODEL）
if changed:
    for alias in ('ANTHROPIC_DEFAULT_SONNET_MODEL', 'ANTHROPIC_DEFAULT_OPUS_MODEL', 'ANTHROPIC_DEFAULT_HAIKU_MODEL'):
        env.pop(alias, None)
    cfg['env'] = env
    with open(sf, 'w') as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
PYEOF
}
sync_claude_settings

# ═══════════════════════════════════════════
# 创建/修复绑定锁（首次成功运行后，写入两个位置）
# 任一 lock 文件缺失就重新生成（保证防绕过完整性）
# ═══════════════════════════════════════════
if [ -f "$LIB_DIR/binding.sh" ]; then
    if [ ! -f "$LOCK_FILE" ]; then
        bash "$LIB_DIR/binding.sh" create "$SCRIPT_DIR" "$LOCK_FILE" 2>/dev/null
        if [ -f "$LOCK_FILE" ]; then
            echo "  [ok] 已绑定到当前设备。解绑命令：./ClaudePortable.command --unlock"
        fi
    fi
    # 镜像 lock 缺失就补上（防止用户手动删除单个文件绕过绑定）
    if [ ! -f "$PORTABLE_CCS/.bind" ]; then
        bash "$LIB_DIR/binding.sh" create "$SCRIPT_DIR" "$PORTABLE_CCS/.bind" 2>/dev/null
    fi
fi

# ═══════════════════════════════════════════
# 启动 Claude Code
# ═══════════════════════════════════════════
echo "  架构: $ARCH | 数据: 便携包内"
echo ""

"$BIN_DIR/claude" "$@"
CLAUDE_EXIT=$?

# 提前清理：删 symlink 和 run lock（不依赖 trap 在 read 之后才跑）。
# 这样即便用户在 read -p 等待时拔 U 盘，主目录也已经干净，
# 不会留下指向不可达 USB 路径的 broken symlink。
[ -L "$SYS_CCS" ] && rm "$SYS_CCS" 2>/dev/null
[ -L "$SYS_CLAUDE" ] && rm "$SYS_CLAUDE" 2>/dev/null
[ -d "$RUN_LOCK" ] && rm -rf "$RUN_LOCK"

# 如果 claude 异常退出，留窗口给用户看错误
if [ $CLAUDE_EXIT -ne 0 ]; then
    echo ""
    echo "  Claude 退出码: $CLAUDE_EXIT"
    read -p "  按回车关闭窗口... " _
fi
exit $CLAUDE_EXIT
