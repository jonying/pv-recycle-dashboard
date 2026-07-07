"""
报废光伏组件回收价值测算模型
============================
基于组件各成分含量 × 回收率 × 市场实时价格，动态计算回收报价

数据源：
- 上海有色网(SMM) quotecenter API: 金(au)/银(ag)/铜(cu)/铝(al)/锡(sn) 期货实时价格
- 每1小时自动更新一次
"""

import json
import time
import logging
import requests
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("PVRecycle")

# ============================================================
# 1. 光伏组件成分含量模型（基于学术文献与行业实测数据）
# ============================================================

# 标准参考面板: 60片电池, 单晶硅PERC, 尺寸1.6m², 重量约20kg, 功率540-580W
# 用户可通过参数自定义面板规格

PANEL_DEFAULTS = {
    "name": "标准60片单晶硅PERC组件",
    "weight_kg": 20.0,        # 总重量 kg
    "area_m2": 1.6,           # 面板面积 m²
    "cell_count": 60,         # 电池片数量
    "power_w": 540,           # 标称功率 W
    "cell_type": "mono_perc", # 电池类型: mono_perc / mono / poly / thin_film
}

# 各成分含量模型 — 每种组件类型的成分占比和绝对含量
# 绝对含量（克）更精确，质量占比作为辅助参考
COMPOSITION_MODEL = {
    "mono_perc": {
        "label": "单晶硅PERC组件",
        "components": {
            "glass":        {"name": "钢化玻璃",     "mass_pct": 70.0,  "content_g": 14000,  "unit": "g",  "recovery_rate": 0.95, "price_unit": "元/吨",  "price_per_unit_kg": 0.50},
            "aluminum":     {"name": "铝边框",       "mass_pct": 10.0,  "content_g": 2000,   "unit": "g",  "recovery_rate": 0.98, "price_unit": "元/吨",  "price_per_unit_kg": 16.0},
            "silicon":      {"name": "硅片(电池片)", "mass_pct": 5.0,   "content_g": 1000,   "unit": "g",  "recovery_rate": 0.85, "price_unit": "元/kg",  "price_per_unit_kg": 90},
            "silver":       {"name": "银(导电银浆)", "mass_pct": 0.036, "content_g": 7.2,    "unit": "g",  "recovery_rate": 0.90, "price_unit": "元/克",  "price_per_unit_kg": None},  # 融通金实时
            "copper":       {"name": "铜(焊带+接线盒)", "mass_pct": 1.25, "content_g": 250,   "unit": "g",  "recovery_rate": 0.95, "price_unit": "元/吨",  "price_per_unit_kg": 65},
            "eva":          {"name": "EVA封装胶膜",  "mass_pct": 5.0,   "content_g": 1000,   "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},     # 目前不可回收
            "backsheet":    {"name": "背板(TPT/PET)", "mass_pct": 2.5,  "content_g": 500,    "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},     # 目前不可回收
            "junction_box": {"name": "接线盒(塑料)",  "mass_pct": 1.0,  "content_g": 200,    "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},
        },
        "silver_per_cell_mg": 120,   # PERC每片电池银耗 ~120mg
        "silver_per_watt_mg": 7.5,   # PERC单瓦银耗 ~7.5mg/W
    },
    "mono": {
        "label": "单晶硅组件(常规)",
        "components": {
            "glass":        {"name": "钢化玻璃",     "mass_pct": 70.0,  "content_g": 14000,  "unit": "g",  "recovery_rate": 0.95, "price_unit": "元/吨",  "price_per_unit_kg": 0.50},
            "aluminum":     {"name": "铝边框",       "mass_pct": 10.0,  "content_g": 2000,   "unit": "g",  "recovery_rate": 0.98, "price_unit": "元/吨",  "price_per_unit_kg": 16.0},
            "silicon":      {"name": "硅片(电池片)", "mass_pct": 5.0,   "content_g": 1000,   "unit": "g",  "recovery_rate": 0.85, "price_unit": "元/kg",  "price_per_unit_kg": 90},
            "silver":       {"name": "银(导电银浆)", "mass_pct": 0.036, "content_g": 6.0,    "unit": "g",  "recovery_rate": 0.90, "price_unit": "元/克",  "price_per_unit_kg": None},
            "copper":       {"name": "铜(焊带+接线盒)", "mass_pct": 1.25, "content_g": 250,   "unit": "g",  "recovery_rate": 0.95, "price_unit": "元/吨",  "price_per_unit_kg": 65},
            "eva":          {"name": "EVA封装胶膜",  "mass_pct": 5.0,   "content_g": 1000,   "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},
            "backsheet":    {"name": "背板(TPT/PET)", "mass_pct": 2.5,  "content_g": 500,    "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},
            "junction_box": {"name": "接线盒(塑料)",  "mass_pct": 1.0,  "content_g": 200,    "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},
        },
        "silver_per_cell_mg": 100,
        "silver_per_watt_mg": 6.5,
    },
    "poly": {
        "label": "多晶硅组件",
        "components": {
            "glass":        {"name": "钢化玻璃",     "mass_pct": 70.0,  "content_g": 14000,  "unit": "g",  "recovery_rate": 0.95, "price_unit": "元/吨",  "price_per_unit_kg": 0.50},
            "aluminum":     {"name": "铝边框",       "mass_pct": 10.0,  "content_g": 2000,   "unit": "g",  "recovery_rate": 0.98, "price_unit": "元/吨",  "price_per_unit_kg": 16.0},
            "silicon":      {"name": "硅片(电池片)", "mass_pct": 5.0,   "content_g": 1000,   "unit": "g",  "recovery_rate": 0.85, "price_unit": "元/kg",  "price_per_unit_kg": 90},
            "silver":       {"name": "银(导电银浆)", "mass_pct": 0.03,  "content_g": 4.8,    "unit": "g",  "recovery_rate": 0.90, "price_unit": "元/克",  "price_per_unit_kg": None},
            "copper":       {"name": "铜(焊带+接线盒)", "mass_pct": 1.25, "content_g": 250,   "unit": "g",  "recovery_rate": 0.95, "price_unit": "元/吨",  "price_per_unit_kg": 65},
            "eva":          {"name": "EVA封装胶膜",  "mass_pct": 5.0,   "content_g": 1000,   "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},
            "backsheet":    {"name": "背板(TPT/PET)", "mass_pct": 2.5,  "content_g": 500,    "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},
            "junction_box": {"name": "接线盒(塑料)",  "mass_pct": 1.0,  "content_g": 200,    "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},
        },
        "silver_per_cell_mg": 80,
        "silver_per_watt_mg": 5.5,
    },
    "thin_film": {
        "label": "薄膜组件(CdTe/CIGS)",
        "components": {
            "glass":        {"name": "钢化玻璃",     "mass_pct": 80.0,  "content_g": 16000,  "unit": "g",  "recovery_rate": 0.95, "price_unit": "元/吨",  "price_per_unit_kg": 0.50},
            "aluminum":     {"name": "铝边框",       "mass_pct": 5.0,   "content_g": 1000,   "unit": "g",  "recovery_rate": 0.98, "price_unit": "元/吨",  "price_per_unit_kg": 16.0},
            "tellurium":    {"name": "碲(CdTe组件)", "mass_pct": 0.05,  "content_g": 10,     "unit": "g",  "recovery_rate": 0.85, "price_unit": "元/克",  "price_per_unit_kg": None},
            "copper":       {"name": "铜(CIGS焊带)", "mass_pct": 0.5,   "content_g": 100,    "unit": "g",  "recovery_rate": 0.95, "price_unit": "元/吨",  "price_per_unit_kg": 65},
            "eva":          {"name": "EVA封装胶膜",  "mass_pct": 5.0,   "content_g": 1000,   "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},
            "backsheet":    {"name": "背板",         "mass_pct": 5.0,   "content_g": 1000,   "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},
            "junction_box": {"name": "接线盒",       "mass_pct": 2.5,   "content_g": 500,    "unit": "g",  "recovery_rate": 0.0,  "price_unit": "元/kg",  "price_per_unit_kg": 0},
        },
        "silver_per_cell_mg": 0,   # 薄膜不含银
        "silver_per_watt_mg": 0,
    },
}

# 回收处理成本系数
PROCESSING_COST = {
    "mechanical_disassembly": 15,   # 机械拆解 元/块
    "chemical_extraction":     8,   # 化学提取 元/块
    "transport_logistics":     5,   # 运输物流 元/块
    "labor":                   10,  # 人工成本 元/块
    "environmental_treatment": 3,   # 环保处理 元/块
    "total_per_panel":        41,   # 合计 元/块
}

# ============================================================
# 2. SMM(上海有色网)统一价格抓取器 — 金/银/铜/铝/锡
# ============================================================

class SMMPriceFetcher:
    """从上海有色网(SMM)quotecenter API获取金属期货实时价格

    API端点:
      1. GET /quotecenter/get_exchange_info?trading_place=shfe,czce
         → 获取所有品种的主力合约ID（每月滚动）
      2. GET /quotecenter/instrument/{instrument_id}/timeline
         → 获取该合约的分时行情（LastPrice, High, Low, PreSettlement等）

    支持品种 (上期所SHFE期货):
      - 黄金(au):  元/克
      - 白银(ag):  元/kg → 换算元/克
      - 铜(cu):    元/吨 → 换算元/kg
      - 铝(al):    元/吨 → 换算元/kg
      - 锡(sn):    元/吨 → 换算元/kg

    回收折扣系数:
      - 贵金属(金/银): 期货价 × 0.95 ≈ 回购价
      - 废铝: 期货价 × 0.75
      - 废铜: 期货价 × 0.80
      - 废锡: 期货价 × 0.85
    """

    QUOTE_CENTER = "https://platform.smm.cn/quotecenter"

    # 金属 → SMM品种代码映射
    METAL_CODE_MAP = {
        "gold":     "au",   # 黄金
        "silver":   "ag",   # 白银
        "copper":   "cu",   # 铜
        "aluminum": "al",   # 铝
        "tin":      "sn",   # 锡
    }

    # 各金属的单位换算与回收折扣
    METAL_CONFIG = {
        "gold":     {"raw_unit": "元/克",  "target_unit": "元/克",  "scrap_discount": 0.95, "divide_by": 1,     "label": "黄金"},
        "silver":   {"raw_unit": "元/kg",  "target_unit": "元/克",  "scrap_discount": 0.95, "divide_by": 1000,  "label": "白银"},
        "copper":   {"raw_unit": "元/吨",  "target_unit": "元/kg",  "scrap_discount": 0.80, "divide_by": 1000,  "label": "铜"},
        "aluminum": {"raw_unit": "元/吨",  "target_unit": "元/kg",  "scrap_discount": 0.75, "divide_by": 1000,  "label": "铝"},
        "tin":      {"raw_unit": "元/吨",  "target_unit": "元/kg",  "scrap_discount": 0.85, "divide_by": 1000,  "label": "锡"},
    }

    # 默认兜底价（API失败时使用）
    DEFAULT_PRICES = {
        "gold":     910.0,
        "silver":   14.5,
        "copper":   80.0,
        "aluminum": 17.0,
        "tin":      340.0,
    }

    def __init__(self, timeout=15):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
            'Accept': 'application/json, text/plain, */*',
            'Referer': 'https://hq.smm.cn/',
        })
        self._instrument_cache = {}  # metal_code → main_instrument_id
        self._cache_time = None

    def _get_main_instruments(self):
        """获取上期所各品种的主力合约ID"""
        if self._instrument_cache and self._cache_time:
            # 缓存1小时内有效
            age = (datetime.now() - self._cache_time).total_seconds()
            if age < 3600:
                return self._instrument_cache

        try:
            resp = self.session.get(
                f"{self.QUOTE_CENTER}/get_exchange_info?trading_place=shfe",
                timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") == 0 and data.get("data"):
                for place in data["data"]:
                    if place.get("trading_place_code") == "shfe":
                        for inst in place.get("instruments", []):
                            metal_code = inst.get("metal_name", "").lower()
                            main_id = inst.get("main_instrument_id", "")
                            if main_id:
                                self._instrument_cache[metal_code] = main_id
                                logger.info(f"SMM主力合约: {inst['instrument_name']}({metal_code}) → {main_id}")

                self._cache_time = datetime.now()
        except Exception as e:
            logger.error(f"SMM获取主力合约失败: {e}")

        return self._instrument_cache

    def fetch_price(self, metal_key):
        """获取单个金属的实时期货价格

        返回包含:
          - futures_price: 期货最新价（原始单位）
          - price_per_kg / price_per_gram: 换算后价格
          - scrap_price: 回收折扣后价格
          - diff / diff_rate: 涨跌
        """
        metal_code = self.METAL_CODE_MAP.get(metal_key)
        if not metal_code:
            logger.warning(f"未知金属: {metal_key}")
            return None

        config = self.METAL_CONFIG[metal_key]
        instruments = self._get_main_instruments()
        instrument_id = instruments.get(metal_code)

        if not instrument_id:
            logger.warning(f"SMM未找到{metal_key}({metal_code})的主力合约，使用默认值")
            return self._build_default(metal_key, config)

        try:
            resp = self.session.get(
                f"{self.QUOTE_CENTER}/instrument/{instrument_id}/timeline",
                timeout=self.timeout
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("code") != 0 or not data.get("data", {}).get("data"):
                logger.warning(f"SMM {metal_key}({instrument_id}): 无分时数据")
                return self._build_default(metal_key, config)

            timeline_data = data["data"]["data"]
            latest = timeline_data[-1]  # 最后一条 = 最新价格

            futures_price = latest.get("LastPrice", 0)
            pre_settle = data["data"].get("pre_settlement_price", 0) or latest.get("PreSettlementPrice", 0)
            high = latest.get("HighestPrice", 0)
            low = latest.get("LowestPrice", 0)
            update_time = latest.get("UpdateTime", "")
            trading_day = latest.get("trading_day", "")

            # 涨跌计算
            if pre_settle and pre_settle > 0:
                diff = round(futures_price - pre_settle, 2)
                diff_rate = round(diff / pre_settle * 100, 2)
            else:
                diff = 0
                diff_rate = 0

            # 单位换算
            converted_price = futures_price / config["divide_by"]
            # 回收折扣
            scrap_price = round(converted_price * config["scrap_discount"], 3)

            logger.info(
                f"SMM {config['label']}({instrument_id}): "
                f"期货={futures_price}{config['raw_unit']} → "
                f"换算={round(converted_price, 3)}{config['target_unit']} → "
                f"回收价={scrap_price}{config['target_unit']} "
                f"(涨跌{diff_rate}%)"
            )

            result = {
                "metal": metal_key,
                "label": config["label"],
                "instrument_id": instrument_id,
                "futures_price": futures_price,
                "futures_unit": config["raw_unit"],
                "converted_price": round(converted_price, 3),
                "scrap_discount": config["scrap_discount"],
                "scrap_price": scrap_price,       # 回收折扣后价格
                "target_unit": config["target_unit"],
                "high": high,
                "low": low,
                "pre_settlement": pre_settle,
                "diff": diff,
                "diff_rate": diff_rate,
                "update_time": update_time,
                "trading_day": trading_day,
                "source": "上海有色网(SMM)期货",
                "note": f"SMM{config['label']}期货{futures_price}{config['raw_unit']} × 折扣{config['scrap_discount']} = 回收价{scrap_price}{config['target_unit']}",
            }

            # 兼容旧接口
            if metal_key == "silver":
                result["buyback_price"] = scrap_price
                result["sell_price"] = round(converted_price * 1.02, 3)
                result["exchange_close"] = futures_price
                result["unit"] = config["target_unit"]
            elif metal_key == "gold":
                result["buyback_price"] = scrap_price
                result["sell_price"] = round(converted_price * 1.02, 2)
                result["exchange_close"] = futures_price
                result["unit"] = config["target_unit"]
            elif metal_key in ("aluminum", "copper"):
                result["price_per_ton"] = round(futures_price * config["scrap_discount"])
                result["price_per_kg"] = scrap_price
                result["new_price_per_ton"] = futures_price
                result["unit"] = config["target_unit"]
            elif metal_key == "tin":
                result["price_per_ton"] = round(futures_price * config["scrap_discount"])
                result["price_per_kg"] = scrap_price
                result["unit"] = config["target_unit"]

            return result

        except Exception as e:
            logger.error(f"SMM {metal_key} 抓取失败: {e}")
            return self._build_default(metal_key, config)

    def _build_default(self, metal_key, config):
        """构建默认兜底价格"""
        default_price = self.DEFAULT_PRICES.get(metal_key, 0)
        logger.warning(f"使用{config['label']}默认参考价: {default_price}{config['target_unit']}")
        result = {
            "metal": metal_key,
            "label": config["label"],
            "instrument_id": "N/A",
            "futures_price": default_price,
            "futures_unit": config["raw_unit"],
            "converted_price": default_price,
            "scrap_discount": config["scrap_discount"],
            "scrap_price": default_price,
            "target_unit": config["target_unit"],
            "high": 0, "low": 0, "pre_settlement": 0,
            "diff": 0, "diff_rate": 0,
            "update_time": "",
            "trading_day": "",
            "source": "默认参考价",
            "note": f"{config['label']}默认参考价 {default_price}{config['target_unit']}",
        }
        # 兼容旧接口
        if metal_key == "silver":
            result["buyback_price"] = default_price
            result["sell_price"] = default_price
            result["unit"] = config["target_unit"]
        elif metal_key in ("aluminum", "copper", "tin"):
            result["price_per_kg"] = default_price
            result["price_per_ton"] = default_price * 1000
            result["unit"] = config["target_unit"]
        return result

    def fetch_all(self):
        """批量获取所有金属（金/银/铜/铝/锡）的实时价格"""
        results = {}
        for metal_key in self.METAL_CODE_MAP:
            result = self.fetch_price(metal_key)
            if result:
                results[metal_key] = result
        return results


# ============================================================
# 3. 价值测算引擎
# ============================================================

class PVRecycleValueCalculator:
    """光伏组件回收价值测算引擎

    数据源: 上海有色网(SMM) quotecenter API
    支持: 金(au)/银(ag)/铜(cu)/铝(al)/锡(sn) 期货实时价格
    """

    def __init__(self):
        self.smm_fetcher = SMMPriceFetcher()
        self.price_cache = {}
        self.last_update_time = None

    def update_prices(self):
        """从SMM获取所有金属期货价格"""
        self.price_cache = self.smm_fetcher.fetch_all()
        self.last_update_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        logger.info(f"价格更新完成 @ {self.last_update_time} (SMM期货API)")
        return self.price_cache

    def get_silver_price_per_gram(self):
        """获取银回收价(元/克)"""
        if "silver" in self.price_cache:
            return self.price_cache["silver"].get("scrap_price",
                   self.price_cache["silver"].get("buyback_price", 13.5))
        return 13.5

    def get_aluminum_price_per_kg(self):
        """获取铝回收价(元/kg)"""
        if "aluminum" in self.price_cache:
            return self.price_cache["aluminum"].get("scrap_price",
                   self.price_cache["aluminum"].get("price_per_kg", 17.0))
        return 17.0

    def get_copper_price_per_kg(self):
        """获取铜回收价(元/kg)"""
        if "copper" in self.price_cache:
            return self.price_cache["copper"].get("scrap_price",
                   self.price_cache["copper"].get("price_per_kg", 80.0))
        return 80.0

    def calculate_panel_value(self, cell_type="mono_perc", panel_params=None):
        """
        计算单块光伏组件的回收价值

        参数:
            cell_type: 电池类型 mono_perc/mono/poly/thin_film
            panel_params: 自定义面板参数(可选)

        返回: 详细价值分析结果
        """
        if panel_params is None:
            panel_params = PANEL_DEFAULTS.copy()
        panel_params["cell_type"] = cell_type

        model = COMPOSITION_MODEL[cell_type]
        components = model["components"]

        # 根据面板参数动态调整银含量
        silver_per_watt = model.get("silver_per_watt_mg", 0)
        silver_per_cell = model.get("silver_per_cell_mg", 0)
        power_w = panel_params.get("power_w", 540)
        cell_count = panel_params.get("cell_count", 60)

        # 银含量优先用功率计算，更精确
        calculated_silver_g = round(power_w * silver_per_watt / 1000, 2) if silver_per_watt > 0 else 0

        result = {
            "panel_info": panel_params,
            "model_type": model["label"],
            "update_time": self.last_update_time,
            "price_sources": {},
            "components": [],
            "total_raw_value": 0,
            "total_recoverable_value": 0,
            "processing_cost": PROCESSING_COST["total_per_panel"],
            "net_recycle_value": 0,
            "recycle_quote_per_watt": 0,
        }

        total_recoverable = 0

        for comp_key, comp_data in components.items():
            content_g = comp_data["content_g"]

            # 动态银含量用计算值覆盖
            if comp_key == "silver" and calculated_silver_g > 0:
                content_g = calculated_silver_g

            recovery_rate = comp_data["recovery_rate"]
            recoverable_g = round(content_g * recovery_rate, 2)

            # 获取对应金属实时价格
            price_info = self._get_component_price(comp_key, content_g)
            unit_price = price_info["unit_price"]
            price_unit_label = price_info["price_unit_label"]

            # 计算价值
            if comp_key in ["silver"]:
                # 银: 含量(克) × 回收率 × 价格(元/克)
                value = round(recoverable_g * unit_price, 2)
            elif comp_key in ["glass"]:
                # 玻璃: 含量(kg) × 回收率 × 价格(元/kg) — 但价格很低
                value = round((recoverable_g / 1000) * unit_price, 2)
            else:
                # 其他金属: 含量(kg) × 回收率 × 价格(元/kg)
                value = round((recoverable_g / 1000) * unit_price, 2)

            # 不可回收材料价值为0
            if recovery_rate == 0:
                value = 0

            total_recoverable += value

            comp_result = {
                "key": comp_key,
                "name": comp_data["name"],
                "content_g": content_g,
                "mass_pct": comp_data["mass_pct"],
                "recovery_rate": recovery_rate,
                "recoverable_g": recoverable_g,
                "unit_price": unit_price,
                "price_unit_label": price_unit_label,
                "price_source": price_info["source"],
                "value_yuan": value,
                "value_rank": 0,  # 后续排序填入
            }
            result["components"].append(comp_result)

        # 按价值排序，填入排名
        sorted_comps = sorted(result["components"], key=lambda x: x["value_yuan"], reverse=True)
        for i, comp in enumerate(sorted_comps):
            comp["value_rank"] = i + 1
        result["components"] = sorted_comps

        result["total_recoverable_value"] = round(total_recoverable, 2)
        result["total_raw_value"] = round(sum(c["content_g"] * c["unit_price"] / (1000 if c["key"] != "silver" else 1) for c in result["components"]), 2)
        result["net_recycle_value"] = round(total_recoverable - PROCESSING_COST["total_per_panel"], 2)
        result["recycle_quote_per_watt"] = round(result["net_recycle_value"] / panel_params["power_w"], 4)

        # 记录价格来源信息
        for comp in result["components"]:
            result["price_sources"][comp["key"]] = comp["price_source"]

        return result

    def calculate_batch_value(self, panel_count, cell_type="mono_perc", panel_params=None):
        """批量计算多块组件的总回收价值"""
        single = self.calculate_panel_value(cell_type, panel_params)
        batch = {
            "panel_count": panel_count,
            "single_panel_value": single,
            "total_recoverable_value": round(single["total_recoverable_value"] * panel_count, 2),
            "total_processing_cost": round(PROCESSING_COST["total_per_panel"] * panel_count, 2),
            "total_net_value": round(single["net_recycle_value"] * panel_count, 2),
            "total_weight_kg": round(PANEL_DEFAULTS["weight_kg"] * panel_count, 2),
        }
        return batch

    def _get_component_price(self, comp_key, content_g):
        """根据成分类型获取对应的实时单价"""
        if comp_key == "silver":
            price = self.get_silver_price_per_gram()
            return {"unit_price": price, "price_unit_label": "元/克(SMM银回收价)", "source": self.price_cache.get("silver", {}).get("source", "SMM")}
        elif comp_key == "aluminum":
            price = self.get_aluminum_price_per_kg()
            return {"unit_price": price, "price_unit_label": "元/kg", "source": self.price_cache.get("aluminum", {}).get("source", "SMM")}
        elif comp_key == "copper":
            price = self.get_copper_price_per_kg()
            return {"unit_price": price, "price_unit_label": "元/kg", "source": self.price_cache.get("copper", {}).get("source", "SMM")}
        elif comp_key == "silicon":
            return {"unit_price": 90, "price_unit_label": "元/kg(回收硅料)", "source": "行业参考"}
        elif comp_key == "glass":
            return {"unit_price": 0.50, "price_unit_label": "元/kg(300-700元/吨)", "source": "行业参考"}
        elif comp_key == "tellurium":
            return {"unit_price": 350, "price_unit_label": "元/克(参考价)", "source": "行业参考"}
        else:
            return {"unit_price": 0, "price_unit_label": "暂无回收渠道", "source": "N/A"}

    def generate_report_json(self, cell_types=["mono_perc", "mono", "poly"], panel_params=None):
        """生成完整分析报告JSON"""
        self.update_prices()

        report = {
            "report_title": "报废光伏组件回收价值测算分析报告",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "price_update_at": self.last_update_time,
            "price_data": self.price_cache,
            "panel_defaults": PANEL_DEFAULTS,
            "processing_cost": PROCESSING_COST,
            "calculations": {},
        }

        for ct in cell_types:
            report["calculations"][ct] = self.calculate_panel_value(ct, panel_params)

        # 价值贡献度分析 — 铝和银的占比
        for ct, calc in report["calculations"].items():
            total = calc["total_recoverable_value"]
            contributions = {}
            for comp in calc["components"]:
                if comp["value_yuan"] > 0:
                    contributions[comp["key"]] = {
                        "value": comp["value_yuan"],
                        "pct_of_total": round(comp["value_yuan"] / total * 100, 1) if total > 0 else 0,
                        "name": comp["name"],
                    }
            calc["value_contribution"] = contributions

        return report


# ============================================================
# 4. 自动定时抓取与更新引擎
# ============================================================

class AutoUpdateEngine:
    """每1小时自动从SMM抓取金属期货价格并更新光伏组件总价值"""

    def __init__(self, interval_seconds=3600, output_dir=None):
        self.interval = interval_seconds  # 默认3600秒=1小时
        self.calculator = PVRecycleValueCalculator()
        self.output_dir = output_dir or Path.cwd()
        self.running = False
        self.history = []
        # 加载已有历史记录，避免覆盖
        history_path = self.output_dir / "price_history.json"
        if history_path.exists():
            try:
                with open(history_path, "r", encoding="utf-8") as f:
                    self.history = json.load(f)
                logger.info(f"已加载 {len(self.history)} 条历史记录")
            except Exception:
                self.history = []

    def run_once(self):
        """执行一次价格更新和价值重算"""
        try:
            prices = self.calculator.update_prices()
            report = self.calculator.generate_report_json()

            # 保存当前报告
            report_path = self.output_dir / "current_report.json"
            with open(report_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2, default=str)

            # 保存价格历史记录（包含金/银/铜/铝/锡5种金属）
            history_entry = {
                "time": report["generated_at"],
                # 5种金属回收价
                "gold_per_gram": prices.get("gold", {}).get("scrap_price", "N/A"),
                "silver_per_gram": prices.get("silver", {}).get("scrap_price", "N/A"),
                "copper_per_kg": prices.get("copper", {}).get("scrap_price", "N/A"),
                "aluminum_per_kg": prices.get("aluminum", {}).get("scrap_price", "N/A"),
                "tin_per_kg": prices.get("tin", {}).get("scrap_price", "N/A"),
                # 期货原始价（用于折线图参考）
                "gold_futures": prices.get("gold", {}).get("futures_price", "N/A"),
                "silver_futures": prices.get("silver", {}).get("futures_price", "N/A"),
                "copper_futures": prices.get("copper", {}).get("futures_price", "N/A"),
                "aluminum_futures": prices.get("aluminum", {}).get("futures_price", "N/A"),
                "tin_futures": prices.get("tin", {}).get("futures_price", "N/A"),
                # 涨跌率
                "gold_diff_rate": prices.get("gold", {}).get("diff_rate", 0),
                "silver_diff_rate": prices.get("silver", {}).get("diff_rate", 0),
                "copper_diff_rate": prices.get("copper", {}).get("diff_rate", 0),
                "aluminum_diff_rate": prices.get("aluminum", {}).get("diff_rate", 0),
                "tin_diff_rate": prices.get("tin", {}).get("diff_rate", 0),
                # 组件回收净值
                "mono_perc_net_value": report["calculations"]["mono_perc"]["net_recycle_value"],
                "mono_net_value": report["calculations"]["mono"]["net_recycle_value"],
                "poly_net_value": report["calculations"]["poly"]["net_recycle_value"],
            }
            self.history.append(history_entry)
            # 只保留最近500条记录
            if len(self.history) > 500:
                self.history = self.history[-500:]

            # 保存历史到文件
            history_path = self.output_dir / "price_history.json"
            with open(history_path, "w", encoding="utf-8") as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2, default=str)

            logger.info(f"报告已更新: 银={history_entry['silver_per_gram']}元/克, "
                        f"铝={history_entry['aluminum_per_kg']}元/kg, "
                        f"铜={history_entry['copper_per_kg']}元/kg, "
                        f"锡={history_entry['tin_per_kg']}元/kg, "
                        f"金={history_entry['gold_per_gram']}元/克, "
                        f"单晶PERC净回收={history_entry['mono_perc_net_value']}元/块")

            return report

        except Exception as e:
            logger.error(f"自动更新执行失败: {e}")
            return None

    def run_loop(self, max_iterations=None):
        """持续运行，每隔 interval_seconds 执行一次"""
        self.running = True
        iteration = 0
        logger.info(f"自动更新引擎启动，间隔 {self.interval} 秒")

        while self.running:
            self.run_once()
            iteration += 1

            if max_iterations and iteration >= max_iterations:
                logger.info(f"达到最大迭代次数 {max_iterations}，停止")
                break

            logger.info(f"等待 {self.interval} 秒后下次更新...")
            time.sleep(self.interval)

    def stop(self):
        self.running = False


# ============================================================
# 5. CLI 入口
# ============================================================

if __name__ == "__main__":
    import sys

    output_dir = Path("E:/Work buddy工作文件夹/pv-recycle-model")

    if len(sys.argv) > 1 and sys.argv[1] == "auto":
        # 自动模式: 每1小时循环更新
        engine = AutoUpdateEngine(interval_seconds=3600, output_dir=output_dir)
        try:
            engine.run_loop()
        except KeyboardInterrupt:
            engine.stop()
            logger.info("引擎已停止")
    elif len(sys.argv) > 1 and sys.argv[1] == "once":
        # 单次模式: 只执行一次
        engine = AutoUpdateEngine(output_dir=output_dir)
        report = engine.run_once()
        if report:
            print(f"\n✅ 报告已生成: {output_dir / 'current_report.json'}")
            pd = report.get('price_data', {})
            print(f"   金回收价: {pd.get('gold', {}).get('scrap_price', 'N/A')} 元/克")
            print(f"   银回收价: {pd.get('silver', {}).get('scrap_price', 'N/A')} 元/克")
            print(f"   铜回收价: {pd.get('copper', {}).get('scrap_price', 'N/A')} 元/kg")
            print(f"   铝回收价: {pd.get('aluminum', {}).get('scrap_price', 'N/A')} 元/kg")
            print(f"   锡回收价: {pd.get('tin', {}).get('scrap_price', 'N/A')} 元/kg")
            for ct, calc in report["calculations"].items():
                print(f"   {calc['model_type']}: 净回收价值={calc['net_recycle_value']} 元/块")
    else:
        # 默认: 单次生成报告
        calc = PVRecycleValueCalculator()
        report = calc.generate_report_json()

        report_path = output_dir / "current_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2, default=str)

        print(f"\n📊 报告已保存到: {report_path}")
        print(f"   价格更新时间: {report['price_update_at']}")
        for ct, calc in report["calculations"].items():
            print(f"\n   【{calc['model_type']}】")
            print(f"   可回收总价值: {calc['total_recoverable_value']} 元/块")
            print(f"   处理成本: {calc['processing_cost']} 元/块")
            print(f"   净回收价值: {calc['net_recycle_value']} 元/块")
            print(f"   每瓦回收价: {calc['recycle_quote_per_watt']} 元/W")
            # 价值贡献TOP3
            top3 = sorted(calc["components"], key=lambda x: x["value_yuan"], reverse=True)[:3]
            for comp in top3:
                if comp["value_yuan"] > 0:
                    pct = round(comp["value_yuan"] / calc["total_recoverable_value"] * 100, 1) if calc["total_recoverable_value"] > 0 else 0
                    print(f"   → {comp['name']}: {comp['value_yuan']}元 (占比{pct}%)")
