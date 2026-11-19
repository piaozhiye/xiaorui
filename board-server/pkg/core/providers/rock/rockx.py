"""RockX2 ctypes 绑定: RockASR / RockTTS 共用的薄封装.

对照头文件(两个 SDK 的 rockx2 版本一致):
  rockx.h / rockx_type.h / types/rockx_audio.h / types/rockx_packet.h
  rockasr.h (RockASR SDK lib/Linux/include)

两个坑(2026-11-18 板上实测, 见 docs/superpowers/specs/2026-11-18-*):
  1. RockXSetParamString 的 needRelease=0 表示 SDK 只存指针不拷贝 —— Python 侧
     必须持有 bytes 引用到 handle 销毁, 否则临时对象被 GC 后 SDK 读到野指针
     (表现为 data_path 变成乱码)。
  2. TTS 回调注册在 node "RKTTSPipelineV1" 上(param 名 RKTTSPipelineV1Callback);
     SDK demo 源码里写的 node 名是错的, 照抄会 init 失败。
"""
import ctypes
import os

ROCKX_KEY_MODULE_NAME = "module_name"
ROCKX_KEY_DATA_PATH = "data_path"
ROCKX_KEY_AUTH_CONFIG = "auth_config"

ROCKX_RET_SUCCESS = 0
ROCKX_CALLBACK_ON_SCHED_THREAD = 0

ROCKX_STREAM_STATE_FIRST = 0
ROCKX_STREAM_STATE_CONTINUE = 1
ROCKX_STREAM_STATE_LAST = 2

ROCKX_DATA_TYPE_INT16 = 3

ROCKASR_MODE_STREAM = 0
ROCKASR_MODE_TRANSCRIBE = 1
ROCKASR_LANG_ENGLISH = 0
ROCKASR_LANG_CHINESE = 1
ASR_STATE_RUNNING = 2
ASR_STATE_FINISH = 3
ASR_STATE_COMPLETE = 4


class RockXAudioWav(ctypes.Structure):
    _fields_ = [
        ("sampleRate", ctypes.c_int),
        ("channels", ctypes.c_int),
        ("state", ctypes.c_int),
        ("timestamp", ctypes.c_ulong),
        ("sampleNum", ctypes.c_int),
        ("dtype", ctypes.c_int),
        ("data", ctypes.c_void_p),
    ]


class RockAsrLangMode(ctypes.Structure):
    _fields_ = [("source", ctypes.c_int), ("target", ctypes.c_int)]


class RockAsrResult(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("state", ctypes.c_int),
        ("tsStart", ctypes.c_ulong),
        ("tsEnd", ctypes.c_ulong),
        ("speakerId", ctypes.c_int),
        ("result", ctypes.c_char_p),
        ("newResult", ctypes.c_char_p),
        ("transResult", ctypes.c_char_p),
        ("audioResult", ctypes.POINTER(RockXAudioWav)),
    ]


class RockXAsrInitParam(ctypes.Structure):
    _fields_ = [
        ("modelName", ctypes.c_char_p),
        ("workMode", ctypes.c_int),
        ("langMode", RockAsrLangMode),
        ("sampleRate", ctypes.c_int),
        ("userdata", ctypes.c_void_p),
        ("resultCallback", ctypes.c_void_p),
    ]


RKTTS_CALLBACK = ctypes.CFUNCTYPE(
    None,
    ctypes.POINTER(ctypes.c_int16),
    ctypes.c_int,
    ctypes.c_bool,
    ctypes.c_void_p,
)
ROCKASR_RESULT_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.POINTER(RockAsrResult), ctypes.c_int, ctypes.c_void_p
)
RESULT_CALLBACK = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p
)


class RockXError(Exception):
    pass


class RockX:
    """一个 RockX2 handle 的封装; 字符串/回调的存活引用挂在 self._keepalive."""

    def __init__(self, lib_path, preload=()):
        for p in preload:
            if not os.path.isfile(p):
                raise FileNotFoundError(f"rockx2 依赖库缺失: {p}")
            ctypes.CDLL(p, mode=ctypes.RTLD_GLOBAL)
        if not os.path.isfile(lib_path):
            raise FileNotFoundError(f"算法库缺失: {lib_path}")
        self.lib = ctypes.CDLL(lib_path, mode=ctypes.RTLD_GLOBAL)
        self._keepalive = []
        self._input_refs = {}
        self._packet_inputs = {}
        self._bind()

    def _keep(self, b):
        self._keepalive.append(b)
        return b

    def _bind(self):
        lib = self.lib
        c, i, f, v = ctypes.c_char_p, ctypes.c_int, ctypes.c_float, ctypes.c_void_p

        lib.RockXCreate.restype = v
        lib.RockXCreate.argtypes = []
        lib.RockXInit.restype = i
        lib.RockXInit.argtypes = [v]
        lib.RockXDestroy.restype = i
        lib.RockXDestroy.argtypes = [v]
        lib.RockXSetParamString.restype = i
        lib.RockXSetParamString.argtypes = [v, c, c, c, i]
        lib.RockXSetParamInt.restype = i
        lib.RockXSetParamInt.argtypes = [v, c, c, i]
        lib.RockXSetParamFloat.restype = i
        lib.RockXSetParamFloat.argtypes = [v, c, c, f]
        lib.RockXSetParamPointer.restype = i
        lib.RockXSetParamPointer.argtypes = [v, c, c, v, i, v]
        lib.RockXSetOutputInfo.restype = i
        lib.RockXSetOutputInfo.argtypes = [v, c, ctypes.POINTER(c), i, v, i, v]
        lib.RockXInputCreate.restype = v
        lib.RockXInputCreate.argtypes = [v]
        lib.RockXInputDestroy.restype = i
        lib.RockXInputDestroy.argtypes = [v, v]
        lib.RockXProcessAsync.restype = i
        # wait 句柄按 SDK 官方 demo 传 NULL(头文件: "can be NULL if no
        # necessary wait")。传真实句柄再 RockXWaitFinish 会 ret=-1 失败
        # (2026-11-19 板端实测), 推理完成信号用回调即可。
        lib.RockXProcessAsync.argtypes = [v, v, ctypes.POINTER(v)]
        lib.RockXInputGetPacketAt.restype = v
        lib.RockXInputGetPacketAt.argtypes = [v, i]
        lib.RockXPacketSetString.restype = i
        lib.RockXPacketSetString.argtypes = [v, c, i]
        lib.RockXPacketSetFloat.restype = i
        lib.RockXPacketSetFloat.argtypes = [v, f]
        lib.RockXPacketSetAudio2.restype = i
        lib.RockXPacketSetAudio2.argtypes = [v, i, i, i, i, i, v]

    # ---- handle ----
    def create(self):
        h = self.lib.RockXCreate()
        if not h:
            raise RockXError("RockXCreate 返回 NULL")
        return h

    def init(self, handle):
        rc = self.lib.RockXInit(handle)
        if rc != ROCKX_RET_SUCCESS:
            raise RockXError(f"RockXInit 失败 ret={rc} (常见原因: 授权过期/模型路径错)")
        return rc

    def destroy(self, handle):
        return self.lib.RockXDestroy(handle)

    # ---- 参数 ----
    def set_str(self, handle, node, name, value):
        if isinstance(value, str):
            value = value.encode()
        return self.lib.RockXSetParamString(
            handle,
            self._keep(node.encode()) if node else None,
            self._keep(name.encode()),
            self._keep(value),
            0,
        )

    def set_int(self, handle, node, name, value):
        return self.lib.RockXSetParamInt(
            handle,
            self._keep(node.encode()) if node else None,
            self._keep(name.encode()),
            value,
        )

    def set_ptr(self, handle, node, name, value, need_release=0):
        return self.lib.RockXSetParamPointer(
            handle,
            self._keep(node.encode()) if node else None,
            self._keep(name.encode()),
            value,
            need_release,
            None,
        )

    def set_output_info(self, handle, name, out_names, cb):
        arr = (ctypes.c_char_p * len(out_names))(
            *[self._keep(n.encode()) for n in out_names]
        )
        return self.lib.RockXSetOutputInfo(
            handle, self._keep(name.encode()), arr, len(out_names), cb,
            ROCKX_CALLBACK_ON_SCHED_THREAD, None,
        )

    # ---- 输入/执行 ----
    def input_create(self, handle):
        inp = self.lib.RockXInputCreate(handle)
        if not inp:
            raise RockXError("RockXInputCreate 返回 NULL")
        self._input_refs[inp] = []
        return inp

    def input_destroy(self, handle, inp):
        rc = self.lib.RockXInputDestroy(handle, inp)
        if rc != ROCKX_RET_SUCCESS:
            raise RockXError(f"RockXInputDestroy 失败 ret={rc}")
        self._input_refs.pop(inp, None)
        self._packet_inputs = {p: owner for p, owner in self._packet_inputs.items()
                               if owner != inp}
        return rc

    def packet_set_string(self, packet, value):
        if isinstance(value, str):
            value = value.encode()
        self._input_refs[self._packet_inputs[packet]].append(value)
        return self.lib.RockXPacketSetString(packet, value, 0)

    def packet_set_float(self, packet, value):
        return self.lib.RockXPacketSetFloat(packet, value)

    def packet_set_pcm(self, packet, pcm, sample_rate, state):
        """pcm: 16k/mono/int16 原始字节; 拷贝进 ctypes 数组, SDK 侧无需释放回调."""
        if len(pcm) % 2:
            pcm = pcm[:-1]
        arr = (ctypes.c_int16 * (len(pcm) // 2)).from_buffer_copy(pcm)
        self._input_refs[self._packet_inputs[packet]].append(arr)
        return self.lib.RockXPacketSetAudio2(
            packet, state, sample_rate, 1, len(arr), ROCKX_DATA_TYPE_INT16, arr
        )

    def process_async(self, handle, input_):
        return self.lib.RockXProcessAsync(handle, input_, None)

    def input_get_packet(self, input_, index=0):
        pkt = self.lib.RockXInputGetPacketAt(input_, index)
        if not pkt:
            raise RockXError(f"RockXInputGetPacketAt({index}) 返回 NULL")
        self._packet_inputs[pkt] = input_
        return pkt


def load_rockx(lib_dir, algo_lib, extra_preload=()):
    """按依赖顺序加载 rockx2 运行时 + 算法库, 返回 RockX.

    lib_dir 里需要: librknnrt.so librockx2.so librockx_modules.so (+ extra_preload)
    algo_lib 是算法库文件名, 如 "librocktts.so" / "librockasr.so"。
    """
    preload = [os.path.join(lib_dir, n) for n in
               ("librknnrt.so", "librockx2.so", "librockx_modules.so")]
    preload += [os.path.join(lib_dir, n) for n in extra_preload]
    return RockX(os.path.join(lib_dir, algo_lib), preload=preload)
