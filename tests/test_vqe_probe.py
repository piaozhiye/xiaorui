"""Validate failure detection with uninstrumented fake SDK writes."""
import os
from pathlib import Path
import resource
import signal
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "tools/vqe-oob-probe"


class ProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.exe = Path(cls.tmp.name) / "probe"
        subprocess.run([os.environ.get("HOST_CC", "cc"), "-std=c11", "-O1", "-g",
                        "-Wall", "-Wextra", "-Werror", str(SOURCE / "capacity_probe.c"),
                        str(SOURCE / "mock_vqe.c"), "-lm", "-o", str(cls.exe)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_probe(self, mode="good", guard="canary", frames="8"):
        def disable_core():
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        return subprocess.run([str(self.exe), "mock-config", frames, guard],
                              env=dict(os.environ, MOCK_VQE_MODE=mode),
                              text=True, capture_output=True, timeout=5, preexec_fn=disable_core)

    def test_good_canary(self):
        p = self.run_probe()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("completed=8 requested=8", p.stdout)

    def test_good_guard_page(self):
        self.assertEqual(self.run_probe(guard="guard-page").returncode, 0)

    def test_real_channel_count_controls_allocation(self):
        p = self.run_probe("two-channels")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("output_channels=2", p.stdout)
        self.assertIn("output_bytes=3072", p.stdout)

    def test_canary_write_returns_failure(self):
        p = self.run_probe("overrun")
        self.assertEqual(p.returncode, 1)
        self.assertIn("out_post=64", p.stderr)
        self.assertNotIn("RESULT PASS", p.stdout)

    def test_uninstrumented_write_hits_guard_page(self):
        p = self.run_probe("overrun", "guard-page")
        self.assertEqual(p.returncode, -signal.SIGSEGV)

    def test_prefix_guard(self):
        p = self.run_probe("underrun")
        self.assertEqual(p.returncode, 1)
        self.assertIn("out_pre=1023", p.stderr)

    def test_invalid_geometry_and_init(self):
        for mode in ("bad-channels", "init-fail"):
            self.assertEqual(self.run_probe(mode).returncode, 2)

    def test_invalid_process_return(self):
        for mode in ("bad-length", "process-fail"):
            p = self.run_probe(mode)
            self.assertEqual(p.returncode, 1)
            self.assertIn("output-length-mismatch", p.stderr)

    def test_invalid_frame_counts(self):
        for frames in ("0", "-1", "abc", "8junk", "100001"):
            self.assertEqual(self.run_probe(frames=frames).returncode, 2)

    def test_plugin_output_and_reload_contract(self):
        exe = Path(self.tmp.name) / "contract"
        subprocess.run([os.environ.get("HOST_CC", "cc"), "-std=c11", "-Wall", "-Wextra",
                        "-Werror", str(ROOT / "vendor/rkaudio-vqe/vqe_contract_test.c"),
                        "-o", str(exe)], check=True)
        subprocess.run([str(exe)], check=True, timeout=5)

    def test_hot_reload_preserves_old_handle_on_geometry_change(self):
        source = (ROOT / "vendor/rkaudio-vqe/pcm_rkaudio_vqe.c").read_text()
        first = source.index("static int vqe_hot_reload(")
        function = source[first:source.index("\n}\n", first) + 3]
        code = '''
#include <assert.h>
#include "vqe_contract.h"
#define RKAUDIO_ERR(...) ((void)0)
#define RKAUDIO_INFO(...) ((void)0)
typedef struct { int parsed; } rkaudio_config_cache_t;
typedef struct {
    rkaudio_config_cache_t config_cache;
    void *vqe_handle; int sample_rate,src_channels,ref_channels,out_channels,vqe_reload_count;
    char *config_path;
} rkaudio_vqe_priv_t;
static int next_ch, old_freed, new_freed, init_fail;
static void *rkaudio_preprocess_init_by_conf(int r,int b,int s,int f,char *p) {
    (void)r;(void)b;(void)s;(void)f;(void)p; return init_fail ? NULL : (void*)2;
}
static int rkaudio_out_chan_invoke(void *h) {(void)h;return next_ch;}
static void rkaudio_preprocess_destory(void *h) {
    if(h==(void*)1) old_freed++; else if(h==(void*)2) new_freed++;
}
static int rkaudio_parse_json_config(rkaudio_vqe_priv_t *p,char *f) {
    (void)f; p->out_channels=2; return 0;
}
'''
        code += function + '''
int main(void) {
    rkaudio_vqe_priv_t p={0}; p.vqe_handle=(void*)1;p.out_channels=1;p.config_cache.parsed=1;
    next_ch=2;
    assert(vqe_hot_reload(&p)==-EINVAL);
    assert(p.vqe_handle==(void*)1 && p.out_channels==1 && old_freed==0 && new_freed==1);
    assert(p.config_cache.parsed==1);
    next_ch=1;init_fail=1;
    assert(vqe_hot_reload(&p)==-1 && p.vqe_handle==(void*)1 && old_freed==0);
    init_fail=0;new_freed=0;
    assert(vqe_hot_reload(&p)==0);
    assert(p.vqe_handle==(void*)2 && p.out_channels==1 && old_freed==1 && new_freed==0);
    assert(p.vqe_reload_count==1);
    return 0;
}
'''
        exe = Path(self.tmp.name) / "hot-reload"
        subprocess.run([os.environ.get("HOST_CC", "cc"), "-std=c11", "-Wall", "-Wextra",
                        "-Werror", "-I" + str(ROOT / "vendor/rkaudio-vqe"),
                        "-x", "c", "-", "-o", str(exe)], input=code, text=True, check=True)
        subprocess.run([str(exe)], check=True, timeout=5)

    def test_plugin_initializes_the_entire_checked_guard(self):
        source = (ROOT / "vendor/rkaudio-vqe/pcm_rkaudio_vqe.c").read_text()
        helpers = source[source.index("#define VQE_PAD_SHORTS"):source.index("static void *vqe_thread_func")]
        first = source.index("pad_fill(priv->input_staging", source.index("static void *vqe_thread_func"))
        startup = source[first:source.index("long frames_seen", first)]
        names = ["input_staging", "output_fifo", "vqe_out_buf", "vqe_process_buf"]
        fields = "\n".join("short *%s; int %s_logic;" % (n, n) for n in names)
        setup = "\n".join("p.%s=b[%d]; p.%s_logic=16;" % (n, i, n) for i, n in enumerate(names))
        code = '#include <stdio.h>\n' + helpers + '\nint main(void) {\n'
        code += 'struct {' + fields + '} p, *priv=&p; short b[4][1040];\n'
        code += 'for(int i=0;i<4;i++) for(int j=0;j<1040;j++) b[i][j]=-1;\n'
        code += setup + '\n' + startup
        code += '''
for(int i=0;i<4;i++) {
    int d=pad_dirty(b[i],16,1040);
    if(d>=0) { printf("guard %d uninitialized at +%d\\n",i,d); return 1; }
    if(pad_dirty_run(b[i],16,1040,0)!=0) return 1;
}
return 0; }
'''
        exe = Path(self.tmp.name) / "guard-init"
        subprocess.run([os.environ.get("HOST_CC", "cc"), "-std=c11", "-Wall", "-Wextra",
                        "-Werror", "-x", "c", "-", "-o", str(exe)], input=code, text=True, check=True)
        p = subprocess.run([str(exe)], text=True, capture_output=True, timeout=5)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
