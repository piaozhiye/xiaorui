import os
import sys
import asyncio
from loguru import logger
from config.config_loader import load_config
from config.settings import check_config_file
from core.utils.cache.manager import cache_manager, CacheType

SERVER_VERSION = "0.9.7"
_logger_initialized = False


def get_module_abbreviation(module_name, module_dict):
    """获取模块名称的缩写，如果为空则返回00
    如果名称中包含下划线，则返回下划线后面的前两个字符
    """
    module_value = module_dict.get(module_name, "")
    if not module_value:
        return "00"
    if "_" in module_value:
        parts = module_value.split("_")
        return parts[-1][:2] if parts[-1] else "00"
    return module_value[:2]


def build_module_string(selected_module):
    """构建模块字符串"""
    return (
        get_module_abbreviation("VAD", selected_module)
        + get_module_abbreviation("ASR", selected_module)
        + get_module_abbreviation("LLM", selected_module)
        + get_module_abbreviation("TTS", selected_module)
        + get_module_abbreviation("Memory", selected_module)
        + get_module_abbreviation("Intent", selected_module)
        + get_module_abbreviation("VLLM", selected_module)
    )


def formatter(record):
    """为没有 tag 的日志添加默认值，并处理动态模块字符串"""
    record["extra"].setdefault("tag", record["name"])
    # 如果没有设置 selected_module，使用默认值
    record["extra"].setdefault("selected_module", "00000000000000")
    # 将 selected_module 从 extra 提取到顶级，以支持 {selected_module} 格式
    record["selected_module"] = record["extra"]["selected_module"]
    return record["message"]


def setup_logging(config=None):
    """从配置文件中读取日志配置，并设置日志输出格式和级别。

    导入期(部分 provider 在模块级调用本函数)若拿不到私有配置, 只提供
    基础 stdout 日志且**不置完成标志** —— 正式启动时 setup_logging() 再
    以真实配置完整初始化; 已按正式配置初始化后, 任何调用不再降级。
    """
    if config is None:
        try:
            check_config_file()
            # 先查缓存，避免在 async 上下文中重复 await load_config
            config = cache_manager.get(CacheType.CONFIG, "main_config")
            if config is None:
                # 缓存也没有（理论上不该发生），才走 asyncio.run
                config = asyncio.run(load_config())
        except OSError:
            # 测试/工具环境可能没有私有配置文件。日志初始化不应成为模块
            # 导入的硬前提 —— 落基础日志让导入继续。
            config = None
    log_config = (config or {}).get("log") or {}
    global _logger_initialized

    if _logger_initialized:
        return logger

    # 使用默认的模块字符串进行初始化
    logger.configure(
        extra={
            "selected_module": log_config.get("selected_module", "00000000000000"),
        }
    )

    log_format = log_config.get(
        "log_format",
        "<green>{time:YYMMDD HH:mm:ss}</green>[{version}_{extra[selected_module]}][<light-blue>{extra[tag]}</light-blue>]-<level>{level}</level>-<light-green>{message}</light-green>",
    )
    log_format_file = log_config.get(
        "log_format_file",
        "{time:YYYY-MM-DD HH:mm:ss} - {version}_{extra[selected_module]} - {name} - {level} - {extra[tag]} - {message}",
    )
    log_format = log_format.replace("{version}", SERVER_VERSION)
    log_format_file = log_format_file.replace("{version}", SERVER_VERSION)

    log_level = log_config.get("log_level", "INFO")
    log_dir = log_config.get("log_dir", "tmp")
    log_file = log_config.get("log_file", "server.log")
    data_dir = log_config.get("data_dir", "data")

    if log_config:
        os.makedirs(log_dir, exist_ok=True)
        os.makedirs(data_dir, exist_ok=True)

    # 配置日志输出
    logger.remove()

    # 输出到控制台
    logger.add(sys.stdout, format=log_format, level=log_level, filter=formatter)

    if log_config:
        # 输出到文件 - 统一目录，按大小轮转（基础兜底不建文件）
        logger.add(
            os.path.join(log_dir, log_file),
            format=log_format_file,
            level=log_level,
            filter=formatter,
            rotation="10 MB",  # 每个文件最大10MB
            retention="30 days",  # 保留30天
            compression=None,
            encoding="utf-8",
            enqueue=True,  # 异步安全
            backtrace=True,
            diagnose=True,
        )
        _logger_initialized = True  # 只有正式配置算完成初始化, 兜底可被覆盖

    return logger


def create_connection_logger(selected_module_str):
    """为连接创建独立的日志器，绑定特定的模块字符串"""
    return logger.bind(selected_module=selected_module_str)
