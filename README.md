# 擎梦数智｜财报智问 · 技术测试源码

供相关人员审阅技术实现、复现财务问答和运行回归测试。

**完整系统在线测试：[https://qingmeng-v4-trial.streamlit.app/](https://qingmeng-v4-trial.streamlit.app/)**

**下载技术源码包：[qingmeng-technical-testing-20261014.zip](dist/qingmeng-technical-testing-20261014.zip)**。也可使用 GitHub 的 **Code → Download ZIP** 下载仓库。

本仓库只提供业务技术层、命令行入口、公开财务数据及测试用例。**网站 UI、页面布局、配色、Logo、GSAP/WebGL/Canvas 动效、视频素材、图表设计和 PDF 封面排版均未公开，也不包含在压缩包中。** 在线界面单独维护，本仓库不能直接作为完整 Streamlit 网站部署。

## 版本与测试结果

- 技术包：`2026.10.14-tech1`，来自本地 `4.20261014.27-controls` 系统的业务代码。
- 50 家企业，2023—2025 年共 150 条企业年度记录；在原有 10 家基础上新增 40 家跨行业企业。
- 本地技术测试 **82/82 通过**，内置业务评测 **55/55 通过**。环境为 Python 3.13；结果摘要见 [VALIDATION.json](VALIDATION.json)。
- 在线链接指向现有独立部署，不代表已部署本技术包对应版本。此次仓库上传不触发或替代线上发布。

## 1. 准备环境

建议使用 **Python 3.13**，在解压后的仓库根目录运行以下命令。

Windows PowerShell：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe cli.py --data-dir ./runtime init
```

macOS / Linux：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python cli.py --data-dir ./runtime init
```

后文的 `python` 指上述虚拟环境解释器；也可先激活虚拟环境。安装依赖需要网络，安装后 CLI 分析默认完全离线，不需要 API Key。`--data-dir` 是全局参数，必须放在子命令之前；默认写入当前工作目录下的 `runtime`，请使用专用测试目录。

初始化会创建 SQLite 测试库并加载内置数据。重复执行只补缺，不覆盖已有测试数值和来源；不会自动迁移其他目录的业务数据库。

## 2. 快速验证

```bash
python cli.py --data-dir ./runtime companies
python cli.py --data-dir ./runtime companies --query 600519
python cli.py --data-dir ./runtime ask "贵州茅台2024年营业收入是多少？"
python cli.py --data-dir ./runtime ask "2024年营业收入是多少？" --company "工业富联"
python cli.py --data-dir ./runtime compare "美的" "格力" --year 2024 --metrics revenue net_profit
python cli.py --data-dir ./runtime risk "五粮液" --year 2024
python cli.py --data-dir ./runtime sources 600519 --year 2024 --metric revenue
python cli.py --data-dir ./runtime schema
```

预期：企业总数为 50；茅台 2024 年营业收入约为 **1,708.99 亿元**；来源能定位到 `sample_600519.csv`，并保留公开数据 URL、披露日期和上游字段。财务数据口径与来源说明见 [BUILTIN_DATA_SOURCES.md](data/BUILTIN_DATA_SOURCES.md)。

`compare` 指定 `--metrics` 时只比较相应指标；不指定时调用原系统的四维规则评分。评分和风险规则是可审阅的规则计算，不代表缺失数据已补齐，也不能把不同行业的指标直接当作同一评价标准。

输出为严格 JSON：缺失值是 `null`。退出码 `0` 表示命令处理完成，还应检查业务 `status`；`2` 表示输入错误，`1` 表示执行失败或评测未全部通过。

## 3. 多轮对话

新建 UTF-8 文件 `questions.json`：

```json
["贵州茅台2024年营业收入是多少？", "那2023年呢？", "换成五粮液2024年呢？"]
```

```bash
python cli.py --data-dir ./runtime chat --input questions.json
```

同一文件内按顺序继承企业、年度和指标；不同文件调用不共享会话。歧义或缺少条件会返回 `needs_clarification` 等状态，应补充明确问题后重试。

## 4. 运行测试与业务评测

```bash
python -m pytest tests -q -p no:cacheprovider
python cli.py benchmark
python tools/verify_package.py
```

测试使用临时隔离资料库，覆盖导入确认、已有数据保护、来源定位、单位复算、研究档案、任务恢复、多轮问答、只读 SQL 和 CLI 错误处理。`benchmark` 自建临时库，不需要提前运行 `init`，并逐项输出实际结果与预期结果。

`tests/fixtures/byd_2024_key_financials.pdf` 是公开年报的两页财务数据摘页，用来验证真实 PDF 的数值提取和页码引用；出处和 SHA-256 见相邻 `.source.json`。它不是系统的产品设计或导出模板。其余 PDF 测试输入由代码生成纯文字模拟文件。

## 5. 技术结构

| 路径/模块 | 用途 |
|---|---|
| `cli.py` | 离线 JSON 命令行测试入口 |
| `core/qa_engine.py`、`conversation.py`、`planner.py` | 问题解析、多轮上下文、任务路由 |
| `core/text_to_sql.py`、`retrieval.py` | 只读查询、原文检索及引用 |
| `core/analysis.py`、`comparability.py`、`scope_probe.py` | 指标分析、风险规则、口径比较 |
| `core/countercheck.py`、`result_audit.py`、`evidence_package.py` | 原件复算、结果审计与证据复演 |
| `core/parsers.py`、`service.py`、`import_changes.py` | 文件解析、预览确认和事务导入 |
| `core/db.py`、`storage.py`、`research_archive.py` | SQLite、工作区隔离和研究档案 |
| `core/agents.py`、`multi_agent_review.py` | 分析执行轨迹及可选协作审查逻辑 |
| `core/llm.py`、`online_disclosure.py`、`downloads.py` | 可选模型/公开披露适配器；离线 CLI 不启用网络 |
| `data/` | 企业目录、年度 CSV、公开来源快照和评测用例 |
| `tests/`、`tools/` | 回归测试、来源数据构建及包完整性校验 |

业务模块可直接调用。导入应遵循 `prepare_import → 人工核对 → commit_import(confirmed=True)`；不能把未确认解析结果视为已入库事实。若自行启用模型或公开披露适配器，需要自己配置服务与网络，不能把密钥写入仓库。

## 6. 在线系统测试路线

1. 打开上方在线链接，从品牌首页进入研究空间。
2. 选择已接入企业和年度，提出具体问题，例如“比亚迪2024年营业收入是多少？”；再追问“那2023年呢？”。线上可选企业范围以其实际部署版本为准。
3. 打开答案来源，核对企业、年度、指标、金额单位和文件位置。
4. 进入口径核验、反证核验、证据复演，检查通过、冲突、未知的依据是否一致。
5. 在数据资产中使用非敏感测试文件执行预览、确认、入库，再查询刚导入的企业。
6. 记录复现步骤、问题文本、期望与实际结果及页面显示版本，再提交仓库 Issue；请勿附 API Key、内部财报或个人信息。

## 数据与公开范围

新增 40 家数据来自公开结构化财务快照，保留原始字段和采集日期，尚未逐份人工核验全部年报原件。未知审计意见保持为空，内置样本身份不等同于核验通过。维护人员可用 `tools/build_builtin_public_data.py --snapshot data/provenance/builtin_public_financials_20261013.json` 离线复现快照数据。

本次仅公开技术测试源码；未随包提供的产品 UI、品牌与视觉资源不在本次公开范围内。技术副本对界面常量依赖做了纯文本替换，未改变财务计算；具体导出边界与改动见 [EXPORT_SCOPE.md](EXPORT_SCOPE.md)。
