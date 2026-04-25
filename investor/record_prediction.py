#!/usr/bin/env python3
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

import db
from domain.services.prediction_orchestrator import parse_predictions
from domain.services.prediction_service import save_predictions

def main():
    # 初始化数据库
    db.init_db()
    
    # 预测数据
    json_str = '''[
        {"code":"sh000001","name":"上证指数","direction":"up","confidence":0.65,"predicted_change":0.15,"strategy_used":"technical","reasoning":"市场处于上升趋势，昨日小幅收涨，技术面支撑良好，但短期有调整压力，预计明日小幅震荡上行"},
        {"code":"sz399001","name":"深证成指","direction":"up","confidence":0.7,"predicted_change":0.25,"strategy_used":"technical","reasoning":"表现相对强势，当前仍保持微涨，技术面支撑较好，资金流入迹象明显，预计明日继续温和上涨"},
        {"code":"sz399006","name":"创业板指","direction":"neutral","confidence":0.6,"predicted_change":-0.1,"strategy_used":"sentiment","reasoning":"波动较大，昨日涨幅最大但当前回调，显示资金轮动特征，短期可能震荡整理，等待方向选择"}
    ]'''
    
    try:
        predictions = parse_predictions(json_str)
        if not predictions:
            print("❌ 未解析到有效预测")
            return

        pred_ids = save_predictions(predictions, model="cron-agent")
        for p, pid in zip(predictions, pred_ids):
            print(f"📝 [{p.get('name', p['code'])}] {p['direction']} (置信度:{p['confidence']:.0%}, 预测涨跌:{p.get('predicted_change', 0):+.2f}%) → ID:{pid}")
        
        print(f"✅ 共记录 {len(predictions)} 条预测")
        
    except Exception as e:
        print(f"❌ 记录预测失败: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()
