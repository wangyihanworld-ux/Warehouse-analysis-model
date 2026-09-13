# 仓储分析模型

一个面向仓储出入库数据的 FIFO、仓储费用和经营分析工具示例。

## 公开版范围

本仓库只包含源码、合成测试和构建入口，不包含任何真实 ERP 数据、真实费率、业务报告、运行数据库、日志、用户设置或 EXE 发布包。

## 本地运行

```powershell
python -m unittest discover -s tests
```

## 一键运行完全脱敏 Demo

首次运行请先安装项目依赖：

```powershell
python -m pip install -r requirements.txt
```

无需准备任何业务文件；下列命令会生成一份可直接打开的合成 Excel 输入数据和完整示例报告：

```powershell
python -m warehouse_analysis.demo
```

文件默认生成到 `demo_output/`（已被 Git 忽略，不会提交）。该目录会包含：

- `合成演示输入数据*.xlsx`：带“演示说明”页的出入库、期初库存和费率数据；仓库、物料、批次、数量和费率均为人为构造。
- `仓储分析报告_*.xlsx`：正式报告链路生成的 12 张工作表，包含 FIFO 批次明细、仓储费用、库龄、月末库存快照、费用敏感性分析和敏感性影响排名。

可指定输出目录：

```powershell
python -m warehouse_analysis.demo --output-dir .\my_demo_output
```

Demo 不读取外部 Excel、数据库、环境变量或网络资源；它只使用代码中固定的合成记录。运行 GUI 前，请准备自己的输入文件和费率配置。不要把真实业务文件、客户名称、合同费率或运行记录提交到版本库。

## 目录

- `warehouse_analysis/`：业务源码
- `tests/`：不依赖真实业务数据的测试
- `packaging/`：Windows 构建入口和配置
- `config/`：本地配置说明；公开版不提供真实费率库

## 数据与安全

请在本地通过环境变量设置管理员密码，并使用自己的脱敏配置。公开仓库不提供生产数据库或生产账号。

## 许可证

当前仓库未附带开源许可证。未经作者另行授权，不默认授予复制、修改或商业使用权。
