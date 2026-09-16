# 王怡涵｜财务分析与商业分析作品集

我正在申请香港经济、商业分析与相关硕士项目，职业方向聚焦财务 BP、FP&A 和商业分析。这里的项目用 Python、Excel 和可复现测试，把业务问题转化为可解释的分析模型。

> 五个核心项目全部使用程序生成的合成数据，不包含真实客户、公司、合同、费率、银行账户或 ERP 明细。项目结果用于展示方法，不构成真实经营预测。

## 核心项目

| 顺序 | 项目 | 解决的问题 | 代表性结果 |
| --- | --- | --- | --- |
| 1 | [仓储成本与经营分析模型](https://github.com/wangyihanworld-ux/Warehouse-analysis-model) | 用 FIFO 重建批次流转，计算仓储费、库龄并检查数量守恒 | 67 项测试；演示仓储费 20,101.90 元、期末库存 375 吨 |
| 2 | [预算差异分析与滚动预测模型](https://github.com/wangyihanworld-ux/Budget-variance-forecast-model) | 将收入与利润差异拆成价格、销量、结构、成本和费用影响 | 22 项测试；收入有利差异 17,584 元、经营利润不利差异 10,724.36 元 |
| 3 | [客户与产品盈利能力分析模型](https://github.com/wangyihanworld-ux/Customer-profitability-analysis-model) | 从收入追溯到贡献利润，识别高收入低利润客户与集中度风险 | 15 项测试；客户甲收入第一但贡献利润率 11.24%，整体为 29.50% |
| 4 | [宏观经济情景与经营预测模型](https://github.com/wangyihanworld-ux/Macroeconomic-scenario-forecast-model) | 连接宏观驱动与收入，进行滚动回测、基准比较和情景预测 | 18 项测试；R² 96.57%，24 个月回测 MAPE 1.28%，含 VIF 与季节朴素基准 |
| 5 | [营运资金与现金流预测模型](https://github.com/wangyihanworld-ux/Working-capital-cashflow-forecast-model) | 用 DSO、DIO、DPO 解释现金转换周期与资金缺口 | 20 项测试；压力缺口 62.63 百万元、增长缺口 19.12 百万元，CCC 68→40 天 |

## 分析方法

- 财务建模：预算差异、滚动预测、贡献利润、营运资金和现金流滚动。
- 数据分析：Python、pandas、NumPy、Excel 报告、回归、情景与敏感性分析。
- 模型治理：输入校验、勾稽与守恒检查、样本外回测、透明基准、边界与局限披露。
- 工程质量：一键合成数据 Demo、自动化测试、GitHub Actions、无真实业务数据。

## 其他项目

- [求职匹配与 STAR 表达规则原型](https://github.com/wangyihanworld-ux/ai-job-match)：浏览器本地运行，清楚区分规则演示与真实 AI。
- [本地事务提醒](https://github.com/wangyihanworld-ux/task-reminder)：纯前端、localStorage、番茄钟及受校验的 JSON 备份导入。

## 联系与使用说明

欢迎通过 GitHub Issues 就模型方法、测试或文档交流。每个核心仓库均提供 README、一键 Demo、合成输入与管理报告；复现前请查看项目内的口径与局限说明。
