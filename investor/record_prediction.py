#!/usr/bin/env python3
"""Record daily prediction to the investor database."""
import sys, os, json
sys.path.insert(0, os.path.dirname(__file__))
import db

db.init_db()

predictions = [
    {
        "code": "sh000001",
        "name": "上证指数",
        "direction": "neutral",
        "confidence": 0.55,
        "predicted_change": 0.15,
        "strategy_used": "technical",
        "reasoning": "上证今日集合竞价微幅高开0.11%，盘中在4100-4119区间窄幅整理，量能温和。上周五天收盘连续在4086-4112区间反复震荡磨底后出现三连阳修复走势，但4118区域（叠加60日均线附近）压力明显。半导体、军工电子等科技板块资金大幅流入（半导体主力净流入169亿），提供了结构性支撑；但北向资金中性偏空、行业整体资金仍为净流出。技术面看，4100-4120是前期密集成交区，突破需更大成交量配合，明日大概率延续震荡整理，等待方向选择。"
    },
    {
        "code": "sz399001",
        "name": "深证成指",
        "direction": "neutral",
        "confidence": 0.5,
        "predicted_change": -0.1,
        "strategy_used": "technical",
        "reasoning": "深成指今日低开0.09%，目前小幅走弱。权重板块出现分化，能源金属（+4.68%）受锂板块带动大幅冲高，但无法有效抵消旅游、电力、钢铁等传统板块的拖累。深市个股涨跌互现，资金整体流出，结构分化明显。日线级别在15000-15200区间徘徊，量能萎缩显示多空均偏谨慎。缺乏明确催化剂，明日预计维持窄幅震荡偏弱格局。"
    },
    {
        "code": "sz399006",
        "name": "创业板指",
        "direction": "down",
        "confidence": 0.6,
        "predicted_change": -0.45,
        "strategy_used": "technical",
        "reasoning": "创业板今日低开0.27%，盘中下探3660后略有回升但整体偏弱。从5分钟K线看开盘后持续走弱，跌破前收盘后未能有效收复。游戏（-1.67%）、电力（-1.41%）等权重板块拖累明显，而创业板权重中的科技股虽有半导体板块强势表现，但资金更多涌入科创板（科创50涨5.19%），对创业板虹吸效应明显。技术形态上，创业板已连续多日在低位运行，短线反弹动能不足，下方支撑3620-3640区间。资金结构与主板分化加剧，明日维持偏弱判断。"
    }
]

ids = []
for p in predictions:
    pid = db.add_prediction(
        target=p["code"],
        direction=p["direction"],
        confidence=p["confidence"],
        reasoning=p["reasoning"],
        strategy_used=p["strategy_used"],
        predicted_change=p["predicted_change"],
        target_name=p["name"]
    )
    ids.append({"code": p["code"], "name": p["name"], "prediction_id": pid})

print(json.dumps(ids, ensure_ascii=False, indent=2))
