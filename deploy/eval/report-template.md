# 审核评测报告

- 生成时间：{{ generated_at }}
- 样本文件：{{ cases_path }}
- 词表文件：{{ terms_path }}
- 模式：{{ mode }}
- 审核模型：{{ model }}

> 本报告由 `deploy/eval` 框架生成。仓库只包含少量无害占位样本。完整的 2,000 条评测集和算法备案材料需要另行提供并在隔离环境中运行，不能把本次结果当作上线验收已完成。

## 汇总

| 指标 | 数值 |
| --- | --- |
| 样本总数 | {{ total }} |
| 拦截（含模型待判定） | {{ blocked_or_pending }} |
| 明确拦截 | {{ blocked }} |
| 本地层未命中、需模型判定 | {{ model_pending }} |
| 放行 | {{ allowed }} |
| 运行错误 | {{ errors }} |
| 应拦截召回率 | {{ catch_rate }} |
| 误伤率（在已标注放行样本上） | {{ false_positive_rate }} |
| 分层一致的应拦截样本 | {{ layer_matches }} / {{ labeled_layer }} |

## 分层计数

{{ layer_table }}

## 说明

- 离线模式不会调用审核模型。输入方向或命中输出复审规则的样本，在 DFA / 正则未命中时记为 `model_pending`。
- 只有带 `expected` 标注的样本才计入召回率和误伤率。
- 完整集请放到 `deploy/eval/data/full-set.jsonl`（已加入 .gitignore），不要提交真实敏感内容。
