import httpx
from bs4 import BeautifulSoup
from config.logger import setup_logging
from plugins_func.register import register_function, ToolType, ActionResponse, Action
from core.utils.util import get_ip_info
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.connection import ConnectionHandler

TAG = __name__
logger = setup_logging()

GET_WEATHER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "获取某个地点的天气，用户应提供一个位置，比如用户说杭州天气，参数为：杭州。"
            "如果用户说的是省份，默认用省会城市。如果用户说的不是省份或城市而是一个地名，默认用该地所在省份的省会城市。"
            "重要：本地未来7天天气已在上下文中提供，用户未指明其他城市时绝对不要调用此工具。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "地点名，例如杭州。可选参数，如果不提供则不传",
                },
                "lang": {
                    "type": "string",
                    "description": "返回用户使用的语言code，例如zh_CN/zh_HK/en_US/ja_JP等，默认zh_CN",
                },
            },
            "required": ["lang"],
        },
    },
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/92.0.4515.107 Safari/537.36"
    )
}

# 天气代码 https://dev.qweather.com/docs/resource/icons/#weather-icons
WEATHER_CODE_MAP = {
    "100": "晴",
    "101": "多云",
    "102": "少云",
    "103": "晴间多云",
    "104": "阴",
    "150": "晴",
    "151": "多云",
    "152": "少云",
    "153": "晴间多云",
    "300": "阵雨",
    "301": "强阵雨",
    "302": "雷阵雨",
    "303": "强雷阵雨",
    "304": "雷阵雨伴有冰雹",
    "305": "小雨",
    "306": "中雨",
    "307": "大雨",
    "308": "极端降雨",
    "309": "毛毛雨/细雨",
    "310": "暴雨",
    "311": "大暴雨",
    "312": "特大暴雨",
    "313": "冻雨",
    "314": "小到中雨",
    "315": "中到大雨",
    "316": "大到暴雨",
    "317": "暴雨到大暴雨",
    "318": "大暴雨到特大暴雨",
    "350": "阵雨",
    "351": "强阵雨",
    "399": "雨",
    "400": "小雪",
    "401": "中雪",
    "402": "大雪",
    "403": "暴雪",
    "404": "雨夹雪",
    "405": "雨雪天气",
    "406": "阵雨夹雪",
    "407": "阵雪",
    "408": "小到中雪",
    "409": "中到大雪",
    "410": "大到暴雪",
    "456": "阵雨夹雪",
    "457": "阵雪",
    "499": "雪",
    "500": "薄雾",
    "501": "雾",
    "502": "霾",
    "503": "扬沙",
    "504": "浮尘",
    "507": "沙尘暴",
    "508": "强沙尘暴",
    "509": "浓雾",
    "510": "强浓雾",
    "511": "中度霾",
    "512": "重度霾",
    "513": "严重霾",
    "514": "大雾",
    "515": "特强浓雾",
    "900": "热",
    "901": "冷",
    "999": "未知",
}


async def fetch_city_info(location, api_key, api_host):
    url = f"https://{api_host}/geo/v2/city/lookup?key={api_key}&location={location}&lang=zh"
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=3.0)) as client:
        response = await client.get(url, headers=HEADERS)
    data = response.json()
    if data.get("error") is not None:
        logger.bind(tag=TAG).error(
            f"获取天气失败，原因：{data.get('error', {}).get('detail')}"
        )
        return None
    return data.get("location", [])[0] if data.get("location") else None


async def fetch_weather_page(url):
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=3.0)) as client:
        response = await client.get(url, headers=HEADERS)
    return BeautifulSoup(response.text, "html.parser") if response.status_code == 200 else None


def parse_weather_info(soup):
    city_name = soup.select_one("h1.c-submenu__location").get_text(strip=True)

    current_abstract = soup.select_one(".c-city-weather-current .current-abstract")
    current_abstract = (
        current_abstract.get_text(strip=True) if current_abstract else "未知"
    )

    current_basic = {}
    for item in soup.select(
        ".c-city-weather-current .current-basic .current-basic___item"
    ):
        parts = item.get_text(strip=True, separator=" ").split(" ")
        if len(parts) == 2:
            key, value = parts[1], parts[0]
            current_basic[key] = value

    temps_list = []
    for row in soup.select(".city-forecast-tabs__row")[:7]:  # 取前7天的数据
        date = row.select_one(".date-bg .date").get_text(strip=True)
        weather_code = (
            row.select_one(".date-bg .icon")["src"].split("/")[-1].split(".")[0]
        )
        weather = WEATHER_CODE_MAP.get(weather_code, "未知")
        temps = [span.get_text(strip=True) for span in row.select(".tmp-cont .temp")]
        high_temp, low_temp = (temps[0], temps[-1]) if len(temps) >= 2 else (None, None)
        temps_list.append((date, weather, high_temp, low_temp))

    return city_name, current_abstract, current_basic, temps_list




# ---------- Open-Meteo 兜底(2026-10-16): 上游演示 key 已死(Authentication
# failed 实测), Open-Meteo 免 key 稳定且板端可达(广州实测)。和风失败时切换。
_WMO_CODE_MAP = {
    0: "晴", 1: "晴间少云", 2: "多云", 3: "阴", 45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 61: "小雨", 63: "中雨",
    65: "大雨", 66: "冻雨", 67: "强冻雨", 71: "小雪", 73: "中雪", 75: "大雪",
    77: "雪粒", 80: "小阵雨", 81: "阵雨", 82: "强阵雨", 85: "小阵雪",
    86: "阵雪", 95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


def _wmo(code):
    return _WMO_CODE_MAP.get(code, "未知")


def _http_get_json(url, params):
    """urllib 直连(板上实测: httpx TLS 握手超时, urllib 同机同域名正常)。"""
    import json as _json
    import ssl
    import urllib.parse
    import urllib.request
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "xr-weather/1.0"})
    with urllib.request.urlopen(req, timeout=8,
                                context=ssl.create_default_context()) as resp:
        return _json.loads(resp.read().decode("utf-8"))


async def _open_meteo_report(location):
    """免 key 天气报告(geocoding + 实时 + 3日), 失败抛异常由调用方兜。"""
    import asyncio
    geo = await asyncio.to_thread(
        _http_get_json, "https://geocoding-api.open-meteo.com/v1/search",
        {"name": location, "count": 1, "language": "zh"})
    results = geo.get("results") or []
    if not results:
        return None
    r = results[0]
    name = f"{r.get('name', location)}({r.get('admin1') or r.get('country') or ''})"
    wx = await asyncio.to_thread(
        _http_get_json, "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": r["latitude"], "longitude": r["longitude"],
            "current": "temperature_2m,weather_code,relative_humidity_2m,"
                       "apparent_temperature,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": "auto", "forecast_days": 3,
        })
    cur, day = wx["current"], wx["daily"]
    report = (f"您查询的位置是：{name}\n\n当前天气: {_wmo(cur['weather_code'])} "
              f"{cur['temperature_2m']}°C(体感 {cur['apparent_temperature']}°C)\n"
              f"详细参数：\n  · 相对湿度: {cur['relative_humidity_2m']}%\n"
              f"  · 风速: {cur['wind_speed_10m']} km/h\n\n未来3天预报：\n")
    import datetime
    today = datetime.date.today()
    for i in range(len(day["time"])):
        d = datetime.date.fromisoformat(day["time"][i])
        tag = "今天" if d == today else ("明天" if d == today + datetime.timedelta(days=1)
                                         else f"{d.month}月{d.day}日")
        report += (f"{tag}: {_wmo(day['weather_code'][i])}，"
                   f"气温 {day['temperature_2m_min'][i]:.0f}~"
                   f"{day['temperature_2m_max'][i]:.0f}°C\n")
    report += "\n（如需某一天的具体天气，请告诉我日期）"
    return report

@register_function("get_weather", GET_WEATHER_FUNCTION_DESC, ToolType.SYSTEM_CTL)
async def get_weather(conn: "ConnectionHandler", location: str = None, lang: str = "zh_CN"):
    from core.utils.cache.manager import cache_manager, CacheType

    weather_config = conn.config.get("plugins", {}).get("get_weather", {})
    # Provider credentials belong in private instance config, never in source.
    api_host = weather_config.get("api_host")
    api_key = weather_config.get("api_key")
    default_location = weather_config.get("default_location", "广州")
    client_ip = conn.client_ip

    # 优先使用用户提供的location参数
    if not location:
        # 通过客户端IP解析城市
        if client_ip:
            # 先从缓存获取IP对应的城市信息
            cached_ip_info = cache_manager.get(CacheType.IP_INFO, client_ip)
            if cached_ip_info:
                location = cached_ip_info.get("city")
            else:
                # 缓存未命中，调用API获取
                ip_info = get_ip_info(client_ip, logger)
                if ip_info:
                    cache_manager.set(CacheType.IP_INFO, client_ip, ip_info)
                    location = ip_info.get("city")

            if not location:
                location = default_location
        else:
            # 若无IP，使用默认位置
            location = default_location
    # 尝试从缓存获取完整天气报告
    weather_cache_key = f"full_weather_{location}_{lang}"
    cached_weather_report = cache_manager.get(CacheType.WEATHER, weather_cache_key)
    if cached_weather_report:
        return ActionResponse(Action.REQLLM, cached_weather_report, None)

    # 缓存未命中: 和风优先(可配私有 key), 任何失败切 Open-Meteo 免 key 兜底
    weather_report = None
    try:
        city_info = (
            await fetch_city_info(location, api_key, api_host)
            if api_host and api_key else None
        )
        if city_info:
            soup = await fetch_weather_page(city_info["fxLink"])
            if soup:
                city_name, current_abstract, current_basic, temps_list = parse_weather_info(soup)
                weather_report = f"您查询的位置是：{city_name}\n\n当前天气: {current_abstract}\n"
                if current_basic:
                    weather_report += "详细参数：\n"
                    for key, value in current_basic.items():
                        if value != "0":
                            weather_report += f"  · {key}: {value}\n"
                weather_report += "\n未来7天预报：\n"
                for date, weather, high, low in temps_list:
                    weather_report += f"{date}: {weather}，气温 {low}~{high}\n"
                weather_report += "\n（如需某一天的具体天气，请告诉我日期）"
    except Exception as e:
        logger.bind(tag=TAG).warning(f"和风天气失败, 切Open-Meteo: {e}")

    if weather_report is None:
        try:
            weather_report = await _open_meteo_report(location)
        except Exception as e:
            logger.bind(tag=TAG).error(f"Open-Meteo 兜底也失败: {e}")
            return ActionResponse(Action.REQLLM, None, "请求失败")
        if weather_report is None:
            return ActionResponse(
                Action.REQLLM, f"未找到相关的城市: {location}，请确认地点是否正确", None
            )

    cache_manager.set(CacheType.WEATHER, weather_cache_key, weather_report)
    return ActionResponse(Action.REQLLM, weather_report, None)
