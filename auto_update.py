"""
报废光伏组件价值自动更新脚本 (GitHub Actions版)
================================
从上海有色网(SMM)期货API抓取金/银/铜/铝/锡实时价格，
自动重算光伏组件回收价值，更新dashboard数据文件
"""

import json
import time
import sys
import logging
from datetime import datetime
from pathlib import Path

# 使用脚本所在目录作为项目目录
PROJECT_DIR = Path(__file__).parent
sys.path.insert(0, str(PROJECT_DIR))

from pv_recycle_model import (
    PVRecycleValueCalculator,
    AutoUpdateEngine,
    COMPOSITION_MODEL,
    PANEL_DEFAULTS,
    PROCESSING_COST,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("AutoUpdate")

# ============================================================
# 主执行逻辑
# ============================================================

def run_single_update():
    """执行一次价格抓取和价值重算"""
    calc = PVRecycleValueCalculator()
    prices = calc.update_prices()

    report = calc.generate_report_json()

    # 保存JSON数据文件（供index.html读取）
    report_path = PROJECT_DIR / "current_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)

    # 保存价格历史（追加模式）
    history_path = PROJECT_DIR / "price_history.json"
    history = []
    if history_path.exists():
        try:
            with open(history_path, "r", encoding="utf-8") as f:
                history = json.load(f)
        except:
            history = []

    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        # 5种金属回收价
        "gold_per_gram": prices.get("gold", {}).get("scrap_price", "N/A"),
        "silver_per_gram": prices.get("silver", {}).get("scrap_price", "N/A"),
        "copper_per_kg": prices.get("copper", {}).get("scrap_price", "N/A"),
        "aluminum_per_kg": prices.get("aluminum", {}).get("scrap_price", "N/A"),
        "tin_per_kg": prices.get("tin", {}).get("scrap_price", "N/A"),
        # 期货原始价
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
    history.append(entry)
    # 只保留最近500条记录
    if len(history) > 500:
        history = history[-500:]

    with open(history_path, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2, default=str)

    # 输出摘要
    logger.info("="*60)
    logger.info(f"价格更新完成 @ {entry['time']}")
    logger.info(f"  黄金回收价: {entry['gold_per_gram']} 元/克 (涨跌: {entry.get('gold_diff_rate', 0)}%)")
    logger.info(f"  白银回收价: {entry['silver_per_gram']} 元/克 (涨跌: {entry.get('silver_diff_rate', 0)}%)")
    logger.info(f"  铜回收价: {entry['copper_per_kg']} 元/kg (涨跌: {entry.get('copper_diff_rate', 0)}%)")
    logger.info(f"  铝回收价: {entry['aluminum_per_kg']} 元/kg (涨跌: {entry.get('aluminum_diff_rate', 0)}%)")
    logger.info(f"  锡回收价: {entry['tin_per_kg']} 元/kg (涨跌: {entry.get('tin_diff_rate', 0)}%)")
    logger.info(f"  单晶PERC净回收值: {entry['mono_perc_net_value']} 元/块")
    logger.info(f"  单晶硅净回收值: {entry['mono_net_value']} 元/块")
    logger.info(f"  多晶硅净回收值: {entry['poly_net_value']} 元/块")
    logger.info(f"  数据文件: {report_path}")
    logger.info("="*60)

    return report


# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--once":
            run_single_update()
        elif sys.argv[1] == "--daemon":
            run_loop(3600)
        else:
            interval = int(sys.argv[1])
            run_loop(interval)
    else:
        # 默认: 单次执行
        run_single_update()
