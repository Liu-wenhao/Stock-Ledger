# Stock Ledger

仿 [TradingView Portfolios](https://cn.tradingview.com/portfolios/) 的股票买卖记录与收益率统计（Vue3 + FastAPI + MySQL + global-stock-data）。

## 环境要求

- Node.js 18+
- Python 3.12+
- MySQL 8（当前环境已安装本机服务 `MySQL84`；也可改用 `docker compose up -d`）

## 快速启动

### 1. 数据库

默认连接（见 `backend/.env`）：

```
mysql+pymysql://stock:stockledger123@127.0.0.1:3306/stock_ledger
```

root 密码同为 `stockledger123`。样例数据导入：

```bash
cd backend
python scripts/seed_csv.py
```

### 2. 后端

```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

### 3. 前端

```bash
cd frontend
npm install
npm run dev
```

打开 http://localhost:5173 。样例 CSV 已导入；也可在「交易」页重新上传 `sample-data/Code_20260726.csv`（全量替换）。

> 说明：样例 CSV 仅有 Buy/Sell、无 Deposit，因此现金会为负值（与 TradingView 导入同类数据表现类似）。可在交易页补一条「入金」记录。点击「刷新行情」会通过 global-stock-data 的新浪/腾讯数据源同步美股最新价（请求失败时保留数据库缓存）。

## 功能概览

- 概览：净值曲线、时间区间收益、每日赢家/输家
- 持股：持仓 / 价格 / 表现（财务指标、风险、技术指标预留）
- 交易：CRUD、CSV 导入导出、股息 Tab
- 分析：月度 vs QQQ、累计盈亏
