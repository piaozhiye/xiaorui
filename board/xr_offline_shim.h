/* 从官方 c-api.h v1.13.8 提取的 Offline 识别器结构体(自动生成, 勿手改) */
#ifndef XR_OFFLINE_SHIM_H
#define XR_OFFLINE_SHIM_H
#include <stdint.h>

typedef struct SherpaOnnxFeatureConfig {
  /** Sample rate expected by the model, for example 16000. */
  int32_t sample_rate;

  /** Feature dimension expected by the model, for example 80. */
  int32_t feature_dim;
} SherpaOnnxFeatureConfig;

typedef struct SherpaOnnxOfflineTransducerModelConfig {
  /** Path to the encoder ONNX model. */
  const char *encoder;
  /** Path to the decoder ONNX model. */
  const char *decoder;
  /** Path to the joiner ONNX model. */
  const char *joiner;
} SherpaOnnxOfflineTransducerModelConfig;

typedef struct SherpaOnnxOfflineParaformerModelConfig {
  /** Path to the ONNX model. */
  const char *model;
} SherpaOnnxOfflineParaformerModelConfig;

typedef struct SherpaOnnxOfflineNemoEncDecCtcModelConfig {
  /** Path to the ONNX model. */
  const char *model;
} SherpaOnnxOfflineNemoEncDecCtcModelConfig;

typedef struct SherpaOnnxOfflineWhisperModelConfig {
  /** Path to the encoder ONNX model. */
  const char *encoder;
  /** Path to the decoder ONNX model. */
  const char *decoder;
  /** Optional language hint, for example "en" or "zh". */
  const char *language;
  /** Optional Whisper task such as "transcribe" or "translate". */
  const char *task;
  /** Number of tail padding frames appended internally. */
  int32_t tail_paddings;

  /** Non-zero to enable token-level timestamps when supported by the model. */
  int32_t enable_token_timestamps;

  /** Non-zero to enable Whisper segment-level timestamps. */
  int32_t enable_segment_timestamps;
} SherpaOnnxOfflineWhisperModelConfig;

typedef struct SherpaOnnxOfflineTdnnModelConfig {
  /** Path to the ONNX model. */
  const char *model;
} SherpaOnnxOfflineTdnnModelConfig;

typedef struct SherpaOnnxOfflineSenseVoiceModelConfig {
  /** Path to the ONNX model. */
  const char *model;
  /** Optional language hint. */
  const char *language;
  /** Non-zero to enable inverse text normalization. */
  int32_t use_itn;
} SherpaOnnxOfflineSenseVoiceModelConfig;

typedef struct SherpaOnnxOfflineMoonshineModelConfig {
  /** Path to the preprocessor ONNX model. */
  const char *preprocessor;
  /** Path to the encoder ONNX model. */
  const char *encoder;
  /** Path to the uncached decoder ONNX model. */
  const char *uncached_decoder;
  /** Path to the cached decoder ONNX model. */
  const char *cached_decoder;
  /** Path to the merged decoder ONNX model. */
  const char *merged_decoder;
} SherpaOnnxOfflineMoonshineModelConfig;

typedef struct SherpaOnnxOfflineFireRedAsrModelConfig {
  /** Path to the encoder ONNX model. */
  const char *encoder;
  /** Path to the decoder ONNX model. */
  const char *decoder;
} SherpaOnnxOfflineFireRedAsrModelConfig;

typedef struct SherpaOnnxOfflineDolphinModelConfig {
  /** Path to the ONNX model. */
  const char *model;
} SherpaOnnxOfflineDolphinModelConfig;

typedef struct SherpaOnnxOfflineZipformerCtcModelConfig {
  /** Path to the ONNX model. */
  const char *model;
} SherpaOnnxOfflineZipformerCtcModelConfig;

typedef struct SherpaOnnxOfflineCanaryModelConfig {
  /** Path to the encoder ONNX model. */
  const char *encoder;
  /** Path to the decoder ONNX model. */
  const char *decoder;
  /** Source language hint. */
  const char *src_lang;
  /** Target language hint. */
  const char *tgt_lang;
  /** Non-zero to enable punctuation and capitalization when supported. */
  int32_t use_pnc;
} SherpaOnnxOfflineCanaryModelConfig;

typedef struct SherpaOnnxOfflineWenetCtcModelConfig {
  /** Path to the ONNX model. */
  const char *model;
} SherpaOnnxOfflineWenetCtcModelConfig;

typedef struct SherpaOnnxOfflineOmnilingualAsrCtcModelConfig {
  /** Path to the ONNX model. */
  const char *model;
} SherpaOnnxOfflineOmnilingualAsrCtcModelConfig;

typedef struct SherpaOnnxOfflineMedAsrCtcModelConfig {
  /** Path to the ONNX model. */
  const char *model;
} SherpaOnnxOfflineMedAsrCtcModelConfig;

typedef struct SherpaOnnxOfflineFunASRNanoModelConfig {
  /** Path to the encoder adaptor. */
  const char *encoder_adaptor;
  /** Path to the LLM ONNX model. */
  const char *llm;
  /** Path to the embedding model. */
  const char *embedding;
  /** Path to the tokenizer file. */
  const char *tokenizer;
  /** System prompt. */
  const char *system_prompt;
  /** User prompt. */
  const char *user_prompt;
  /** Maximum number of generated tokens. */
  int32_t max_new_tokens;
  /** Sampling temperature. */
  float temperature;
  /** Top-p sampling threshold. */
  float top_p;
  /** Random seed. */
  int32_t seed;
  /** Optional language hint. */
  const char *language;
  /** Non-zero to enable inverse text normalization. */
  int32_t itn;
  /** Optional hotwords text. */
  const char *hotwords;
} SherpaOnnxOfflineFunASRNanoModelConfig;

typedef struct SherpaOnnxOfflineFireRedAsrCtcModelConfig {
  /** Path to the ONNX model. */
  const char *model;
} SherpaOnnxOfflineFireRedAsrCtcModelConfig;

typedef struct SherpaOnnxOfflineQwen3ASRModelConfig {
  /** Path to the conv-frontend ONNX model. */
  const char *conv_frontend;
  /** Path to the encoder ONNX model. */
  const char *encoder;
  /** Path to the decoder ONNX model (with KV cache). */
  const char *decoder;
  /** Path to the tokenizer directory (e.g. containing `vocab.json`). */
  const char *tokenizer;
  /** Maximum total sequence length supported by the model. */
  int32_t max_total_len;
  /** Maximum number of new tokens to generate. */
  int32_t max_new_tokens;
  /** Sampling temperature. */
  float temperature;
  /** Top-p (nucleus) sampling threshold. */
  float top_p;
  /** Random seed for reproducible sampling. */
  int32_t seed;
  /** Optional comma-separated hotwords (UTF-8, ASCII ','), e.g. @c
   * "foo,bar,baz". */
  const char *hotwords;
} SherpaOnnxOfflineQwen3ASRModelConfig;

typedef struct SherpaOnnxOfflineCohereTranscribeModelConfig {
  /** Path to the encoder ONNX model. */
  const char *encoder;
  /** Path to the decoder ONNX model. */
  const char *decoder;
  /** Optional language hint, for example "en" or "zh". */
  const char *language;
  /** Non-zero to enable punctuation. */
  int32_t use_punct;
  /** Non-zero to enable inverse text normalization. */
  int32_t use_itn;
} SherpaOnnxOfflineCohereTranscribeModelConfig;

typedef struct SherpaOnnxOfflineModelConfig {
  /** Non-streaming transducer model files. */
  SherpaOnnxOfflineTransducerModelConfig transducer;
  /** Non-streaming Paraformer model files. */
  SherpaOnnxOfflineParaformerModelConfig paraformer;
  /** Non-streaming NeMo CTC model files. */
  SherpaOnnxOfflineNemoEncDecCtcModelConfig nemo_ctc;
  /** Whisper model files and options. */
  SherpaOnnxOfflineWhisperModelConfig whisper;
  /** TDNN model files. */
  SherpaOnnxOfflineTdnnModelConfig tdnn;

  /** Path to the tokens file. */
  const char *tokens;
  /** Number of backend threads. */
  int32_t num_threads;
  /** Non-zero to print debug information. */
  int32_t debug;
  /** Execution provider, for example "cpu" or "cuda". */
  const char *provider;
  /** Optional explicit model type override. */
  const char *model_type;
  /** Modeling unit, such as "cjkchar", "bpe", or "cjkchar+bpe". */
  const char *modeling_unit;
  /** Path to the BPE vocabulary file when BPE is used. */
  const char *bpe_vocab;
  /** Path to the TeleSpeech CTC model. */
  const char *telespeech_ctc;
  /** SenseVoice configuration. */
  SherpaOnnxOfflineSenseVoiceModelConfig sense_voice;
  /** Moonshine configuration. */
  SherpaOnnxOfflineMoonshineModelConfig moonshine;
  /** FireRedAsr configuration. */
  SherpaOnnxOfflineFireRedAsrModelConfig fire_red_asr;
  /** Dolphin configuration. */
  SherpaOnnxOfflineDolphinModelConfig dolphin;
  /** Zipformer CTC configuration. */
  SherpaOnnxOfflineZipformerCtcModelConfig zipformer_ctc;
  /** Canary configuration. */
  SherpaOnnxOfflineCanaryModelConfig canary;
  /** WeNet CTC configuration. */
  SherpaOnnxOfflineWenetCtcModelConfig wenet_ctc;
  /** Omnilingual CTC configuration. */
  SherpaOnnxOfflineOmnilingualAsrCtcModelConfig omnilingual;
  /** MedASR configuration. */
  SherpaOnnxOfflineMedAsrCtcModelConfig medasr;
  /** FunASR Nano configuration. */
  SherpaOnnxOfflineFunASRNanoModelConfig funasr_nano;
  /** FireRedAsr CTC configuration. */
  SherpaOnnxOfflineFireRedAsrCtcModelConfig fire_red_asr_ctc;
  /** Qwen3-ASR configuration. */
  SherpaOnnxOfflineQwen3ASRModelConfig qwen3_asr;
  /** Cohere Transcribe configuration. */
  SherpaOnnxOfflineCohereTranscribeModelConfig cohere_transcribe;
} SherpaOnnxOfflineModelConfig;

typedef struct SherpaOnnxOfflineLMConfig {
  /** Path to the language model. */
  const char *model;
  /** Interpolation scale for the language model. */
  float scale;
} SherpaOnnxOfflineLMConfig;

typedef struct SherpaOnnxHomophoneReplacerConfig {
  /** Unused legacy field kept for ABI compatibility. */
  const char *dict_dir;
  /** Path to the lexicon used by the homophone replacer. */
  const char *lexicon;
  /** Path to the replacement rule FST file. */
  const char *rule_fsts;
} SherpaOnnxHomophoneReplacerConfig;

typedef struct SherpaOnnxOfflineRecognizerConfig {
  /** Feature extraction settings. */
  SherpaOnnxFeatureConfig feat_config;
  /** Offline model configuration. */
  SherpaOnnxOfflineModelConfig model_config;
  /** Optional language model configuration. */
  SherpaOnnxOfflineLMConfig lm_config;

  /** Decoding method, for example "greedy_search" or "modified_beam_search". */
  const char *decoding_method;
  /** Number of active paths for modified beam search. */
  int32_t max_active_paths;

  /** Path to a hotwords file. */
  const char *hotwords_file;

  /** Bonus score added to each hotword token. */
  float hotwords_score;
  /** Path to punctuation or text-processing rule FSTs. */
  const char *rule_fsts;
  /** Path to FAR archives used by text-processing rules. */
  const char *rule_fars;
  /** Optional blank penalty applied during decoding. */
  float blank_penalty;

  /** Optional homophone replacement configuration. */
  SherpaOnnxHomophoneReplacerConfig hr;
} SherpaOnnxOfflineRecognizerConfig;

/* KWS (v1.13.8): OnlineModelConfig 布局与 xiaorui_live.c 的 xr_model_t 一致 */
typedef struct {
  int32_t max_active_paths;
  int32_t num_trailing_blanks;
  float keywords_score;
  float keywords_threshold;
  const char *keywords_file;
  const char *keywords_buf;
  int32_t keywords_buf_size;
} xr_kws_tail_t;

#endif
