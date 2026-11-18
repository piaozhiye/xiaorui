#!/bin/bash
# 回滚一次 RK3562 + ES8388 profile 部署。
#
# 用法: bash ports/rk3562-es8388/rollback.sh SERIAL /tmp/opencode/rk3562-es8388/backup-<timestamp>
# 只恢复 manifest 中明确列出的文件/受管目录。仅删除部署前记录为 DIR_ABSENT 的
# 六个受管根目录；之前存在的目录一律保留并恢复备份内容。
set -euo pipefail

SERIAL="${1:-}"
BACKUP="${2:-}"
[ -n "$SERIAL" ] && [ -n "$BACKUP" ] \
    || { echo "用法: $0 SERIAL BACKUP_DIR [--remove-created-data] [--dry-run]" >&2; exit 2; }
REMOVE_CREATED_DATA=0
DRY_RUN=0
for opt in "${@:3}"; do
    case "$opt" in
        --remove-created-data) REMOVE_CREATED_DATA=1 ;;
        --dry-run) DRY_RUN=1 ;;
        *) echo "未知选项: $opt" >&2; exit 2 ;;
    esac
done
[ -s "$BACKUP/manifest" ] \
    || { echo "缺回滚清单: $BACKUP/manifest" >&2; exit 2; }

ADB="adb -s $SERIAL"
allowed_file() {
    case "$1" in
        /etc/asound.conf|/etc/init.d/S98xiaorui|/data/mixer-es8388.sh|\
        /data/xiaorui/xiaorui.env|/data/xiaorui/start-frontend.sh|\
        /data/xiaorui/board-time.sh|/data/xiaorui/.rk3562-es8388-profile|\
        /data/xiaorui/.rk3562-es8388-state|/data/xiaorui-server/xiaorui_volume|\
        /data/xiaorui-gateway/probe-port.py|/data/xr-es8388-probe.py|\
        /data/xiaorui-server/librkllmrt.so|/data/xiaorui-server/lib/libgomp.so.1|\
        /data/xiaorui-gateway/start-board-stack.sh|/data/xiaorui-gateway/data/.config.yaml|\
        /data/xiaorui/bin/xiaorui_live|\
        /data/xiaorui/lib/libsherpa-onnx-c-api.so|/data/xiaorui/lib/libonnxruntime.so|\
        /data/xiaorui/assets/kws/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx|\
        /data/xiaorui/assets/kws/decoder-epoch-12-avg-2-chunk-16-left-64.onnx|\
        /data/xiaorui/assets/kws/joiner-epoch-12-avg-2-chunk-16-left-64.onnx|\
        /data/xiaorui/assets/kws/tokens.txt|/data/xiaorui/assets/kws/keywords.txt|\
        /data/xiaorui-server/board-scripts/xr_ack24k.wav|\
        /data/xiaorui-server/board-scripts/xr_hmm24k.wav|\
        /data/xiaorui-server/board-scripts/xr_fail24k.wav|\
        /data/xiaorui-server/board-scripts/xr_volack24k.wav|\
        /usr/local/lib/libopus.so.0) return 0 ;;
        *) return 1 ;;
    esac
}
MODEL=$($ADB shell 'cat /proc/device-tree/model' 2>/dev/null | tr -d '\0')
case "$MODEL" in
    *RK3562*) : ;;
    *) echo "不是 RK3562 ($MODEL)，停止" >&2; exit 1 ;;
esac
$ADB shell 'test -f /data/xiaorui/.rk3562-es8388-profile' \
    || { echo "目标板没有 ES8388 profile 所有权标记，拒绝停止服务" >&2; exit 1; }

# 先把清单整份读进数组再处理。
# 不能直接 `while read ... done < manifest`: 循环体里的 adb 会继承同一个 stdin,
# 把清单剩余行当输入读走(真 adb shell 会透传 stdin), 结果只回滚第一条。
mapfile -t MANIFEST_LINES < "$BACKUP/manifest"

# 清单列: 状态 <TAB> 板端路径 <TAB> 本地备份名 <TAB> 权限(八进制, 可缺省)
split_manifest_line() {
    local line="$1"
    state=""; remote=""; local_name=""; mode=""
    IFS="$(printf '\t')" read -r state remote local_name mode <<<"$line"
}

# 先完整校验清单与备份文件；备份不齐时不能先停服务再失败。
for line in "${MANIFEST_LINES[@]}"; do
    split_manifest_line "$line"
    [ -n "${remote:-}" ] || continue
    case "$state" in
        PRESENT)
            allowed_file "$remote" \
                || { echo "manifest 包含未受管文件: $remote" >&2; exit 1; }
            # 用 -f 而不是 -s: 部署前就存在的空文件(空的 keywords.txt 等)
            # 备份回来也是空的, 用 -s 会误报成"备份文件丢失"。
            [ -f "$BACKUP/files/$local_name" ] \
                || { echo "备份文件丢失: $BACKUP/files/$local_name" >&2; exit 1; }
            ;;
        ABSENT)
            allowed_file "$remote" \
                || { echo "manifest 包含未受管文件: $remote" >&2; exit 1; }
            ;;
        SYMLINK)
            [ "$remote" = /usr/local/lib/libopus.so.0 ] \
                || { echo "manifest 包含未受管链接: $remote" >&2; exit 1; }
            # 目标必须是系统库路径。相对路径按链接所在目录 /usr/local/lib 解析,
            # "../lib/libopus.so.0" 恰好指回自己(断链), 不能接受;
            # 绝对路径允许 /usr 与 /lib 下的任意系统库, 不能只认两个字面量。
            case "$local_name" in
                /usr/lib*/*.so*|/lib*/*.so*|/usr/lib64*/*.so*) : ;;
                *) echo "opus 链接目标不在白名单: $local_name" >&2; exit 1 ;;
            esac
            [ "$local_name" != "$remote" ] \
                || { echo "opus 链接指回自身: $local_name" >&2; exit 1; }
            ;;
        DIR_PRESENT)
            : ;;
        DIR_ABSENT)
            case "$remote" in
                /data/miniforge|/data/gwvenv|/data/wheels|/data/xiaorui|\
                /data/xiaorui-server|/data/xiaorui-gateway) : ;;
                *) echo "不允许回滚未受管目录: $remote" >&2; exit 1 ;;
            esac
            ;;
        *)
            echo "manifest 状态无效: $state" >&2
            exit 1
            ;;
    esac
done

# dry-run: 清单已校验通过, 只打印将要做的动作, 不停服务、不改板。
# 用来在真机上先确认"这次回滚到底会动哪些东西"。
if [ "$DRY_RUN" = "1" ]; then
    echo "== dry-run: 只校验并打印计划, 不停服务、不改板 (serial=$SERIAL) =="
    echo "== 计划: 停止本 profile 的 xiaorui_live / llm_rkllm.py / server/app.py =="
    if $ADB shell 'pgrep -x xiaorui_live >/dev/null 2>&1'; then
        echo "  当前前端在运行 -> 回滚后会自动拉起板内栈与前端"
    else
        echo "  当前前端没在运行 -> 回滚后保持停止状态"
    fi
    for candidate in /data/xiaorui/.rk3562-es8388-mixer-original "$BACKUP/mixer-state.txt"; do
        [ -f "$candidate" ] || continue
        if [ "$candidate" = /data/xiaorui/.rk3562-es8388-mixer-original ]; then
            $ADB pull "$candidate" "$BACKUP/mixer-original.txt" >/dev/null 2>&1 || continue
            candidate="$BACKUP/mixer-original.txt"
        fi
        [ -s "$candidate" ] || continue
        echo "  还原 mixer 现状: $(tr '\n' ' ' < "$candidate")"
        break
    done
    for line in "${MANIFEST_LINES[@]}"; do
        split_manifest_line "$line"
        [ -n "${remote:-}" ] || continue
        case "$state" in
            PRESENT)
                echo "  恢复文件 $remote (备份 $local_name, 权限 ${mode:-644})"
                ;;
            ABSENT)
                echo "  删除本次新建 $remote"
                ;;
            SYMLINK)
                echo "  恢复链接 $remote -> $local_name"
                ;;
            DIR_PRESENT)
                echo "  保留部署前已存在目录 $remote"
                ;;
            DIR_ABSENT)
                if [ "$REMOVE_CREATED_DATA" = "1" ]; then
                    echo "  删除部署前不存在的目录 $remote"
                else
                    echo "  保留新建目录 $remote (需 --remove-created-data 才删除)"
                fi
                ;;
        esac
    done
    exit 0
fi

FAILURES=0

echo "== 仅停止此 profile 的服务 (serial=$SERIAL) =="
# 回滚前先记住前端是否在跑: 回滚完要把它拉回来, 否则板子会从"能说话"
# 变成"完全静默", 比回滚前更糟。
FRONTEND_WAS_RUNNING=0
$ADB shell 'pgrep -x xiaorui_live >/dev/null 2>&1' && FRONTEND_WAS_RUNNING=1
# 只停本 profile 的进程: 按完整路径匹配 cmdline, 不像 pkill -f "[s]erver/app.py"
# 那样命中板上任何一个 app.py。
# ★ 模式串用字符类错开自身: adb shell '... pkill -f "/data/.../app.py" ...' 的
#   远端 shell 自己的命令行里就含这个模式, pkill 会把承载它的 shell 一起杀掉
#   -> adb 返回非零 -> 本脚本 set -e 直接中断, 后面几步全不执行。
$ADB shell 'pkill -x xiaorui_live 2>/dev/null || true'
$ADB shell 'pkill -f "/data/xiaorui-server/llm_rkll[m].py" 2>/dev/null || true'
$ADB shell 'pkill -f "/data/xiaorui-gateway/server/[a]pp.py" 2>/dev/null || true'
# rock helper 是独立进程(app.py 只是父), 不停会占着 600MB+ NPU 内存
$ADB shell 'pkill -f "rock/[a]sr_helper.py" 2>/dev/null || true'
$ADB shell 'pkill -f "rock/[t]ts_helper.py" 2>/dev/null || true'
# 等前端真的退出: 它还在的时候 init 脚本不会拉新的, 结果谁也没在跑。
$ADB shell 'i=0; while [ $i -lt 60 ] && pgrep -x xiaorui_live >/dev/null 2>&1; do sleep 0.5 2>/dev/null || sleep 1; i=$((i+1)); done'

echo "== 按 manifest 恢复配置文件 =="
for line in "${MANIFEST_LINES[@]}"; do
    split_manifest_line "$line"
    [ -n "${remote:-}" ] || continue
    case "$state" in
        PRESENT)
            allowed_file "$remote"
            local_file="$BACKUP/files/$local_name"
            [ -f "$local_file" ] || {
                echo "备份文件丢失，停止以免造成部分回滚: $local_file" >&2
                exit 1
            }
            $ADB shell "mkdir -p '$(dirname "$remote")'" </dev/null
            $ADB push "$local_file" "$remote" >/dev/null </dev/null
            # adb push 不保留执行位：按备份时的权限还原，否则脚本会变成不可执行，
            # 后续 deploy.sh 的"已安装 profile"判定 (test -x) 会拒绝重跑。
            if [ -n "${mode:-}" ]; then
                case "$mode" in
                    [0-7][0-7][0-7]|[0-7][0-7][0-7][0-7]) : ;;
                    *) echo "manifest 权限列无效: $mode ($remote)" >&2; exit 1 ;;
                esac
                $ADB shell "chmod $mode '$remote'" </dev/null
            fi
            echo "恢复: $remote"
            ;;
        ABSENT)
            # 只删除部署前确认不存在、由本 profile 新建的单文件。
            allowed_file "$remote"
            $ADB shell "rm -f '$remote'" </dev/null
            echo "移除本次新建: $remote"
            ;;
        SYMLINK)
            $ADB shell "mkdir -p '$(dirname "$remote")' && ln -sfn '$local_name' '$remote'" </dev/null
            echo "恢复链接: $remote -> $local_name"
            ;;
        DIR_PRESENT)
            echo "保留部署前已存在目录: $remote"
            ;;
        DIR_ABSENT)
            # 仅六个白名单受管根目录可由 manifest 指示清理。
            case "$remote" in
                /data/miniforge|/data/gwvenv|/data/wheels|/data/xiaorui|\
                /data/xiaorui-server|/data/xiaorui-gateway) : ;;
                *) echo "不允许移除未受管目录: $remote" >&2; exit 1 ;;
            esac
            # 默认保留新安装的大型运行时/模型，避免删除部署后用户加的数据。
            # 明确传 --remove-created-data 才清理部署前确认不存在的根目录。
            if [ "$REMOVE_CREATED_DATA" = "1" ]; then
                $ADB shell "rm -rf '$remote'" </dev/null
                echo "移除部署前不存在的受管目录: $remote"
            else
                echo "保留新建目录(需显式 --remove-created-data 才清理): $remote"
            fi
            ;;
        *)
            echo "manifest 状态无效: $state" >&2
            exit 1
            ;;
    esac
done

# 还原 mixer: 本 profile 改过麦 PGA 和喇叭音量, 文件回滚不覆盖它们。
# 优先用板端留存的"装本 profile 之前"的那份; 没有才退回本次备份里的当前值。
MIXER_ORIGINAL=/data/xiaorui/.rk3562-es8388-mixer-original
MIXER_SRC="$BACKUP/mixer-state.txt"
if $ADB shell "test -f $MIXER_ORIGINAL" >/dev/null 2>&1; then
    if $ADB pull "$MIXER_ORIGINAL" "$BACKUP/mixer-original.txt" >/dev/null 2>&1 \
        && [ -s "$BACKUP/mixer-original.txt" ]; then
        MIXER_SRC="$BACKUP/mixer-original.txt"
        echo "== mixer 还原源: 板端留存的初始值 =="
    fi
fi
if [ -s "$MIXER_SRC" ]; then
    MIXER_CARD=$($ADB shell 'cat /proc/asound/cards' 2>/dev/null | tr -d '\0' \
        | awk '/^[[:space:]]*[0-9]+ \[/ {print $1; exit}')
    if [ -n "${MIXER_CARD:-}" ]; then
        echo "== 还原 mixer 现状 =="
        while IFS='|' read -r ctl val; do
            [ -n "${ctl:-}" ] || continue
            case "$ctl" in
                "Main Mic"|"Speaker"|"spk switch")
                    # 开关型控件只能用 sset 的位置参数(name= 不认)。
                    $ADB shell "amixer -c $MIXER_CARD sset '$ctl' '$val'" </dev/null \
                        || { echo "  还原开关失败: $ctl" >&2; FAILURES=$((FAILURES + 1)); }
                    ;;
                *)
                    $ADB shell "amixer -c $MIXER_CARD cset name='$ctl' '$val'" </dev/null \
                        || { echo "  还原控件失败: $ctl" >&2; FAILURES=$((FAILURES + 1)); }
                    ;;
            esac
            echo "  mixer: $ctl = $val"
        done < "$MIXER_SRC"
    else
        echo "!! 找不到声卡号，mixer 未还原" >&2
    fi
else
    echo "== 无 mixer 备份，跳过还原 =="
fi

# 把服务拉回来: 回滚前的板子是能说话的, 回滚后不能只剩一堆文件。
if [ "$FRONTEND_WAS_RUNNING" = "1" ]; then
    if $ADB shell 'test -x /etc/init.d/S98xiaorui' >/dev/null 2>&1; then
        echo "== 拉起板内栈与前端 (回滚前前端在运行) =="
        if ! $ADB shell 'bash /etc/init.d/S98xiaorui start' </dev/null; then
            echo "!! 自动拉起失败，请手工执行: adb -s $SERIAL shell 'bash /etc/init.d/S98xiaorui start'" >&2
            FAILURES=$((FAILURES + 1))
        elif ! $ADB shell 'pgrep -x xiaorui_live >/dev/null 2>&1'; then
            echo "!! 拉起后前端进程仍不存在，请手工排查" >&2
            FAILURES=$((FAILURES + 1))
        fi
    else
        echo "!! 无 /etc/init.d/S98xiaorui，请手工拉起板内栈与前端" >&2
        FAILURES=$((FAILURES + 1))
    fi
else
    echo "== 回滚前前端未运行，保持停止状态 =="
fi

if [ "$REMOVE_CREATED_DATA" = "1" ]; then
    for d in /data/xiaorui-server/rockasr /data/xiaorui-server/rocktts; do
        if $ADB shell "test -d $d" >/dev/null 2>&1; then
            $ADB shell "rm -rf $d" </dev/null
            echo "移除 RockASR/RockTTS SDK 目录: $d"
        fi
    done
fi

if [ "$FAILURES" -eq 0 ]; then
    echo "回滚完成。默认保留大模型/运行时目录；确认只含本 profile 资产时可传 --remove-created-data 清理。备份: $BACKUP。"
else
    echo "回滚结束, 但有 $FAILURES 项失败(见上面 stderr)。备份: $BACKUP。" >&2
    exit 1
fi
