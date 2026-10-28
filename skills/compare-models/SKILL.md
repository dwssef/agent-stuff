---
name: compare-models
description: Compare specified AI models using Artificial Analysis data, especially token prices, Intelligence Index task cost, Intelligence/Coding/Agentic indices, and other requested metrics. Use when choosing between models based on price, quality, or performance.
---

# 模型性价比对比

优先使用 Artificial Analysis 官方 REST API 比较模型的价格、能力和性能；必要时以其网页补充。默认用中文回答；遵循用户指定的语言和格式。

## 查询数据

需要 Python 3 和已配置的环境变量 ARTIFICIAL_ANALYSIS_API_KEY。密钥只通过请求头发送，不要写进文件、命令参数或输出。缺少密钥时，说明可在 [Artificial Analysis Data API](https://artificialanalysis.ai/data-api) 申请并配置；仍可使用网页上可验证的数据，并标注来源。

使用本 skill 所在目录的脚本。实际调用时，将脚本路径替换为已读取 skill 的绝对路径：

~~~bash
python3 /path/to/compare-models/scripts/compare_models.py "Claude Sonnet 4" "GPT-4.1"
~~~

脚本通过官方 API 查询模型目录，处理全部分页，并输出 JSON。遇到名称不唯一或无法确认的模型时，使用返回的候选列表向用户澄清，不要自行挑选近似型号。

默认指标：

- 每百万 Token 输入价与输出价（USD）
- AA Intelligence Index 每项任务成本（USD）
- Intelligence、Coding、Agentic 指数
- 输出速度、首 Token 时间、首个回答 Token 时间、端到端响应时间

按用户当次要求，用 --include 增加指标或用 --exclude 暂时隐藏指标。可用指标名：

- 价格：input-price、output-price、cache-hit-price、cache-write-price、price-blended-3-to-1、price-blended-7-to-2-to-1
- 能力：intelligence、coding、agentic、finance-accounting、strategy-ops、legal、healthcare-medical、engineering、economics、openness-index
- 性能：output-speed、time-to-first-token、time-to-first-answer-token、end-to-end-response-time
- 其他：context-window、parameters-total、parameters-active、open-weights
- 单项基准：benchmark:<API evaluation field>，例如 benchmark:terminalbench_v4_0

示例：

~~~bash
python3 /path/to/compare-models/scripts/compare_models.py "Claude Sonnet 4" "GPT-4.1" --include output-speed context-window
python3 /path/to/compare-models/scripts/compare_models.py "Claude Sonnet 4" "GPT-4.1" --exclude agentic
~~~

仅当请求指标需要时才调用 Pro+ 模型端点。若当前 API 套餐不支持，保留可用数据并明确标注缺失原因；不要把缺失值当作 0。

## 汇总对比

读取脚本 JSON，按价格、能力、速度和延迟分组，用紧凑的 Markdown 表格比较。按用户给出的顺序显示数据、单位和相对第一款模型的差异百分比，并指出更优的一方；把多项共同缺失的数据合并说明，避免大量空行。根据问题选取相关领域指标和少数具体 benchmark（如编程问题优先编程与 Agent 基准），标明测试名称；用户要求详细数据时再展开。默认以表格为主，用户需要图表或历史走势时再提供。

API 缺少相关数据时，查看 Artificial Analysis 网页补充可核实的数据，链接具体页面并标明来源、测量条件和口径。网页与 API 数值冲突时分别呈现，不强行合并；无法访问、需要登录或数据不可用时说明原因，不猜测。网页的历史走势、提示类型或延迟拆分仅在确实能核实时展示，不假定 API 与网页覆盖相同。

task-cost 是 Artificial Analysis Intelligence Index 加权任务的成本，不等同于用户实际工作负载的成本。说明该口径，并避免据此推算用户的真实账单。不要将不同指标加权合成为一个自创总分；用一两句话概括价格、能力之间的取舍。

保留 API 返回的 Intelligence Index 版本。若用户指定了领域或基准，只依据对应的能力指数或 benchmark 数据给出判断。

## 故障处理

- 缺少密钥、认证失败、套餐限制、限流、网络错误或响应格式异常时，简洁报告具体原因和下一步。
- 不输出密钥，不显示完整请求头，不将 API 错误解释为模型数值为零。
- 对模型未覆盖的指标或不可用数据明确标注；不得悄悄混用数据来源或口径。

数据字段以 [Artificial Analysis API 参考](https://artificialanalysis.ai/data-api/docs/reference) 为准。
