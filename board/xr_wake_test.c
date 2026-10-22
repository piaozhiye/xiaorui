#include "xr_wake.h"
#include <assert.h>

int main(void) {
    char cmd[128];
    /* 窗内再唤醒必须匹配全文；不能丢掉真实指令前缀。 */
    assert(xr_is_wake_only_kws("小运小玉"));
    assert(xr_is_wake_only_kws("小玉小运"));
    assert(xr_is_wake_only_kws("哎，小瑞小瑞。"));
    assert(xr_is_wake_only_kws("小瑞"));
    assert(!xr_is_wake_only_kws("介绍一下小瑞"));
    assert(!xr_is_wake_only_kws("我想了解小瑞小瑞"));
    assert(!xr_is_wake_only_kws("小瑞帮我个忙"));
    assert(!xr_is_wake_only_kws("小运小红")); /* 不扩全局变体表 */
    assert(!xr_is_wake_only_kws("小A小瑞"));
    assert(!xr_is_wake_only_kws("小"));
    assert(!xr_is_wake_only_kws(NULL));
    /* 纯唤醒的形状兜底: 变体表只收了 14 个实测听岔字, 表外的听岔
     * (实测"小看小看")必须仍被认成纯唤醒, 否则会被当指令上云 ->
     * 服务端再判一次(两端表不一致) -> 唤醒词被喂给 LLM(实测回"🙂"),
     * LLM 的 TTS 还会把随后的指令段吃掉(实测云 ASR 只听到 "Okay")。 */
    assert(xr_is_wake_only_shape("小看小看"));
    assert(xr_is_wake_only_shape("小瑞小瑞"));
    assert(xr_is_wake_only_shape("哎，小绿小绿。"));
    assert(xr_is_wake_only_shape("小月小月"));
    assert(!xr_is_wake_only_shape("小瑞小瑞帮我开灯"));
    assert(!xr_is_wake_only_shape("小A小瑞"));
    assert(!xr_is_wake_only_shape("小瑞"));
    assert(!xr_is_wake_only_shape("介绍一下小瑞"));
    assert(!xr_is_wake_only_shape(NULL));
    assert(xr_cloud_asr_wake_action("小看小看") == 1);
    assert(xr_cloud_asr_wake_action("小瑞小瑞开灯") == 2);
    /* 拉丁形态的唤醒词(ASR 吐 Latin): 前端要自己认出来, 别白绕一次云往返 */
    assert(xr_is_wake_only_latin("xiaorui"));
    assert(xr_is_wake_only_latin("XiaoRui"));
    assert(xr_is_wake_only_latin("你好xiaorui"));
    assert(xr_is_wake_only_latin("你好 Xiaorui"));
    assert(!xr_is_wake_only_latin("xiaorui帮我开灯"));
    assert(!xr_is_wake_only_latin("小瑞小瑞"));
    assert(!xr_is_wake_only_latin(NULL));
    assert(xr_parse_wake_text("xiaorui", 0, cmd, sizeof(cmd)) == 1);
    assert(xr_parse_wake_text("XiaoRui", 0, cmd, sizeof(cmd)) == 1);
    assert(xr_parse_wake_text("你好xiaorui", 0, cmd, sizeof(cmd)) == 1);
    assert(xr_parse_wake_text("xiaorui", 1, cmd, sizeof(cmd)) == 1);
    assert(xr_parse_wake_text("xiaorui帮我开灯", 0, cmd, sizeof(cmd)) == 2);
    assert(!strcmp(cmd, "帮我开灯"));
    assert(xr_parse_wake_text("XiaoRui帮我关灯", 0, cmd, sizeof(cmd)) == 2);
    assert(!strcmp(cmd, "帮我关灯"));
    /* 实测听岔形态: 本地 RKNN 把唤醒词转成"小想"/"小看看看"。
       双单元要能本地认出来; 单单元走"交云端判定"候选。 */
    assert(xr_parse_wake_text("小想小想", 0, cmd, sizeof(cmd)) == 1);
    assert(xr_parse_wake_text("小看小看", 0, cmd, sizeof(cmd)) == 1);
    assert(xr_is_wake_candidate("小想"));
    assert(xr_is_wake_candidate("小看看看"));
    assert(xr_is_wake_candidate("小瑞小瑞"));
    assert(xr_is_wake_candidate("XiaoRui"));
    assert(!xr_is_wake_candidate("帮我开灯"));
    assert(!xr_is_wake_candidate("今天天气怎么样"));
    assert(!xr_is_wake_candidate(""));
    assert(xr_parse_wake_text("小瑞小翠叫什么名", 0, cmd, sizeof(cmd)) == 2);
    assert(!strcmp(cmd, "叫什么名"));
    assert(xr_parse_wake_text("小小瑞小贵叫什么名字", 0, cmd, sizeof(cmd)) == 2);
    assert(!strcmp(cmd, "叫什么名字"));
    assert(xr_parse_wake_text("小队小队", 0, cmd, sizeof(cmd)) == 1);
    assert(xr_parse_wake_text("哎小月小月", 0, cmd, sizeof(cmd)) == 1);
    assert(xr_parse_wake_text("小瑞小瑞，你叫什么名字？", 0, cmd, sizeof(cmd)) == 2);
    assert(!strcmp(cmd, "你叫什么名字？"));
    assert(xr_parse_wake_text("小瑞，小瑞。", 0, cmd, sizeof(cmd)) == 1);
    assert(xr_parse_wake_text("今天小瑞小瑞", 0, cmd, sizeof(cmd)) == 0);
    assert(xr_parse_wake_text("再讲一个笑话", 0, cmd, sizeof(cmd)) == 0);
    assert(xr_parse_wake_text("再讲一个笑话", 1, cmd, sizeof(cmd)) == 2);
    assert(!strcmp(cmd, "再讲一个笑话"));
    /* 跟说窗内再喊唤醒词 = 重新纯唤醒, 不当指令送 LLM(2026-10-12 声学闭环) */
    assert(xr_parse_wake_text("小瑞小瑞", 1, cmd, sizeof(cmd)) == 1);
    assert(xr_parse_wake_text("哎小瑞小瑞", 1, cmd, sizeof(cmd)) == 1);
    assert(xr_parse_wake_text("小瑞小瑞。", 1, cmd, sizeof(cmd)) == 1);
    /* 窗内"唤醒词+指令"仍整句送 LLM(服务端自行剥唤醒词) */
    assert(xr_parse_wake_text("小瑞小瑞帮我个忙", 1, cmd, sizeof(cmd)) == 2);
    assert(!strcmp(cmd, "小瑞小瑞帮我个忙"));
    /* 礼貌前缀 + 唤醒词(实测语料: "麻烦小瑞小瑞听我说"/"请问小瑞小瑞你在吗") */
    assert(xr_parse_wake_text("麻烦小瑞小瑞听我说", 0, cmd, sizeof(cmd)) == 2);
    assert(!strcmp(cmd, "听我说"));
    assert(xr_parse_wake_text("请问小瑞小瑞你在吗", 0, cmd, sizeof(cmd)) == 2);
    assert(!strcmp(cmd, "你在吗"));
    assert(xr_parse_wake_text("你好小瑞小瑞", 0, cmd, sizeof(cmd)) == 1);
    assert(xr_parse_wake_text("哎，小队小队，打开空调", 0, cmd, sizeof(cmd)) == 2);
    assert(!strcmp(cmd, "打开空调"));
    /* 近音负例不得唤醒: 敌对语料实测样本 */
    assert(xr_parse_wake_text("祥瑞", 0, cmd, sizeof(cmd)) == 0);
    assert(xr_parse_wake_text("小爱同学", 0, cmd, sizeof(cmd)) == 0);
    assert(xr_parse_wake_text("小小小", 0, cmd, sizeof(cmd)) == 0);
    assert(xr_parse_wake_text("你叫小瑞吗", 0, cmd, sizeof(cmd)) == 0);
    assert(xr_parse_wake_text("水鬼", 0, cmd, sizeof(cmd)) == 0);
    assert(xr_parse_wake_text("小院小院你好", 0, cmd, sizeof(cmd)) == 2);
    /* 句中双单元查找(仅声学 KWS 命中时由前端调用) */
    {
        const char *r;
        r = xr_find_wake_mid("马拉山小瑞小瑞听我说");
        assert(r && !strcmp(r, "听我说"));
        r = xr_find_wake_mid("问小瑞小瑞跟我说");
        assert(r && !strcmp(r, "跟我说"));
        r = xr_find_wake_mid("麻烦小瑞小瑞");
        assert(r && !*r);
        assert(xr_find_wake_mid("小迪小张小王") == NULL);
        assert(xr_find_wake_mid("小小小") == NULL);
        assert(xr_find_wake_mid("小爱同学你好") == NULL);
        assert(xr_find_wake_mid("祥瑞") == NULL);
    }
    /* KWS 命中时的单单位放宽档: 仅前端在 kws_wake_this_utt 时调用。
       ASR 抖动掉一个字时双单元门会整轮丢弃(实测"瑞小瑞你叫什么名字"), 故放宽到
       单单位; 找不到任何唤醒单元仍不放行。 */
    {
        const char *r;
        r = xr_find_wake_once("瑞小瑞你叫什么名字");
        assert(r && !strcmp(r, "你叫什么名字"));
        r = xr_find_wake_once("小瑞你叫什么名字");
        assert(r && !strcmp(r, "你叫什么名字"));
        r = xr_find_wake_once("麻烦小瑞听我说");
        assert(r && !strcmp(r, "听我说"));
        r = xr_find_wake_once("小瑞");
        assert(r && !*r);              /* 无尾巴 => 纯唤醒 */
        r = xr_find_wake_once("小瑞好");
        assert(r && !strcmp(r, "好"));  /* 单字尾巴由调用方拒(回声特征) */
        assert(xr_find_wake_once("水鬼") == NULL);
        assert(xr_find_wake_once("小爱同学你好") == NULL);   /* 小爱 不在变体表 */
        assert(xr_find_wake_once("今天天气不错") == NULL);
        assert(xr_find_wake_once("") == NULL);
        /* UTF-8 字符计数: 单单位放宽档要求尾巴 >=2 字 */
        assert(xr_utf8_chars("") == 0);
        assert(xr_utf8_chars("好") == 1);
        assert(xr_utf8_chars("好的") == 2);
        assert(xr_utf8_chars("abc") == 3);
    }
    /* KWS 命中时的唤醒门: 双单元优先, 单单位兜底(护栏: 尾巴 >=2 字) */
    {
        int single = 0;
        assert(xr_parse_wake_kws("小院小路", cmd, sizeof cmd, &single) == 1);
        assert(!cmd[0]);
        assert(xr_parse_wake_kws("小路小院", cmd, sizeof cmd, &single) == 1);
        assert(xr_parse_wake_kws("小运小玉", cmd, sizeof cmd, &single) == 1);
        assert(xr_parse_wake_text("小院小路", 0, cmd, sizeof cmd) == 0);
        assert(xr_parse_wake_kws("介绍一下小院小路", cmd, sizeof cmd, &single) != 1);
        assert(xr_parse_wake_kws("小院小路打开灯", cmd, sizeof cmd, &single) == 2);
        assert(xr_parse_wake_kws("瑞小瑞你叫什么名字", cmd, sizeof cmd, &single) == 2);
        assert(single == 1 && !strcmp(cmd, "你叫什么名字"));
        assert(xr_parse_wake_kws("小瑞你叫什么名字", cmd, sizeof cmd, &single) == 2);
        assert(single == 1 && !strcmp(cmd, "你叫什么名字"));
        assert(xr_parse_wake_kws("问小瑞小瑞跟我说", cmd, sizeof cmd, &single) == 2);
        assert(single == 0 && !strcmp(cmd, "跟我说"));      /* 双单元仍是首选 */
        assert(xr_parse_wake_kws("小瑞小瑞", cmd, sizeof cmd, &single) == 1);
        assert(single == 0);                                  /* 纯唤醒走双单元 */
        assert(xr_parse_wake_kws("瑞小瑞", cmd, sizeof cmd, &single) == 1);
        assert(single == 1);                                  /* 单单位 + 无尾巴 */
        assert(xr_parse_wake_kws("小瑞好", cmd, sizeof cmd, &single) == 0);
        assert(xr_parse_wake_kws("瑞小瑞好", cmd, sizeof cmd, &single) == 0);
        assert(xr_parse_wake_kws("水鬼", cmd, sizeof cmd, &single) == 0);
        assert(xr_parse_wake_kws("小爱同学你好", cmd, sizeof cmd, &single) == 0);
        assert(xr_parse_wake_kws("今天天气不错", cmd, sizeof cmd, &single) == 0);
        assert(xr_parse_wake_kws("", cmd, sizeof cmd, &single) == 0);
        assert(xr_parse_wake_kws(NULL, cmd, sizeof cmd, &single) == 0);
        cmd[0] = 'X';
        int rc = xr_parse_wake_kws("水鬼", cmd, sizeof cmd, &single);
        assert(rc == 0 && cmd[0] == 0);   /* 忽略时不得污染 out */
    }
    return 0;
}
