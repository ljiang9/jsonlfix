# jsonlfix

修复 LLM 输出的残缺 JSON / JSONL。LLM 喜欢给 JSON 穿衣服——套代码围栏、加注释、
留多余逗号、用单引号、键不加引号、吐 Python 字面量、说到一半截断——这个工具按顺序
逐项修好，每项修复都如实上报。

Python 3.10+，**只用标准库，零依赖**，单个文件即插即用。

## 安装

```bash
# 零依赖，clone 下来就能用
python -m jsonlfix --help
```

## 快速开始

```bash
# JSONL 模式：逐行修复（默认）
python -m jsonlfix in.jsonl -o out.jsonl

# 管道：stdin -> stdout
cat messy.jsonl | python -m jsonlfix > clean.jsonl

# 整文件视为单个 JSON（适合多行围栏块）
python -m jsonlfix --json fenced.txt -o fixed.json

# 只校验不修复
python -m jsonlfix --check in.jsonl

# 显示每处修改的前后对比（输出到 stderr，不污染管道）
python -m jsonlfix --diff in.jsonl -o out.jsonl
```

## 修复项（按应用顺序，每项都会上报）

| # | 修复 | 输入 | 输出 |
|---|------|------|------|
| 1 | 去除代码围栏/前后杂文 | ```` ```json {"a":1} ``` ```` / `结果：{"a":1}请查收` | `{"a":1}` |
| 2 | 移除注释（字符串内的 `//` 不受影响） | `{"a":1 /*x*/} //y` | `{"a":1}` |
| 3 | 去除多余逗号 | `{"a":1,}` / `[1,2,]` | `{"a":1}` / `[1,2]` |
| 4 | 单引号转双引号（分词处理，`it's` 不会误伤） | `{'a':'it\'s'}` | `{"a":"it's"}` |
| 5 | 补全未加引号的键名 | `{name:"x"}` | `{"name":"x"}` |
| 6 | Python 字面量转换 | `{"ok":True,"v":None}` | `{"ok":true,"v":null}` |
| 7 | 截断补全（best-effort，标记 truncated） | `{"a":[1,2` | `{"a":[1,2]}` |
| 8 | 控制字符/智能引号规范化 | `"“hi”"` / 裸 `\x01` | `"hi"` |

合法 JSON 走一遍会**原样返回**（幂等），可放心重复跑。

## 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 成功（`--check`：全部有效） |
| 1 | 有行无效 / 无法修复（`--check`：存在无效行，并列出行号与原因） |
| 2 | 用法错误或 IO 错误（文件读不到、stdin 是终端等） |

修不好的行会**保留原文**（不丢数据），行号保持对齐，原因打到 stderr。

## 已知限制（诚实版）

- **截断补全是 best-effort**：只能补齐括号/引号，流式截断丢掉的半截数据找不回来；
  触发时会标记 `truncated`，下游请按需复核。
- **单引号里的裸撇号**（如 `'don't'`）用启发式处理：前后都是单词字符的引号视为撇号；
  极端嵌套场景可能分词偏差，`--diff` 可复核。
- 字符串里再套一层 JSON 文本不会二次解析（只修外层结构）。
- 超大文件会整读进内存；GB 级文件建议先切分。
- JSONL 模式要求每行一条记录；多行围栏块请用 `--json` 模式。

## License

MIT，见 [LICENSE](LICENSE)。
