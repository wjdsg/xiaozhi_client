# 教材目录数据说明

数据只读复制自 `_review_dictation_robot/server/data/`：

- `chinese_textbook.json`：语文 12 册、91 单元、301 课，按 JSON 的
  `words` 数组逐项统计为 **3265 条**。
- `english_textbook.json`：英语 PEP 8 册、46 单元，逐项统计为
  **952 条**。

源项目 README 宣称“语文 12 册 3198 字”，但当前 JSON 实际有 3265
个数组条目，其中还包含 337 个多字词语条目，因此 3198 不是当前文件
可复算出的真实条目数。API 同时返回 `declaredEntryCount`（源项目宣称）
和 `entryCount`（当前文件真实统计），避免把宣传计数当成数据校验结果。

源文件没有出版年份，因此 `edition` 明确标记为“未标注出版年份”，不做
推测。后续替换正式授权数据时应补充准确的版次/年份。
