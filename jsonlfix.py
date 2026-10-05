#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""jsonlfix —— 修复 LLM 输出的残缺 JSON / JSONL。

LLM 喜欢给 JSON 穿衣服：套代码围栏、加注释、留多余逗号、
用单引号、键不加引号、吐 Python 字面量、说到一半截断。
本工具按顺序逐项修复，每项修复都会如实上报。

只用标准库，零依赖。Python 3.10+。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field

# ---------------------------------------------------------------- 修复项名称
R_FENCE = "去除代码围栏/前后杂文"
R_COMMENT = "移除注释"
R_COMMA = "去除多余逗号"
R_SQUOTE = "单引号转双引号"
R_KEY = "补全未加引号的键名"
R_LITERAL = "Python字面量转换"
R_TRUNC = "截断补全"
R_CLEAN = "控制字符/智能引号规范化"

# 修复项的规范上报顺序
_ORDER = [R_FENCE, R_COMMENT, R_COMMA, R_SQUOTE,
          R_KEY, R_LITERAL, R_TRUNC, R_CLEAN]

# 可作引号定界符的字符（含智能引号）
_DQ = '"\u201c\u201d'      # 双引号家族
_SQ = "'\u2018\u2019"      # 单引号家族

_WORDCH_RE = re.compile(r"\w", re.UNICODE)
_WORD_RE = re.compile(r"[\w$]+")
_NUM_RE = re.compile(r"-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_FENCE_LINE_RE = re.compile(r"^\s*```.*$")
_PARTIAL_WORD_RE = re.compile(r"(?<![\w$])(tru?|fals?|nul?)$", re.IGNORECASE)
_PARTIAL_NUM_RE = re.compile(r"(?<![\w$.])[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?)?$")
_DANGLING_KV_RE = re.compile(
    r",\s*[{[]?\s*\"(?:[^\"\\]|\\.)*\"\s*:\s*$"  # , "k": / , {"k": -> 整个删掉
    r"|([{])\s*\"(?:[^\"\\]|\\.)*\"\s*:\s*$"      # {"k": -> 保留 {，补成空对象
)

_ESCAPES = {
    '"': '"', "\\": "\\", "/": "/",
    "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
    "'": "'",
}


@dataclass
class FixResult:
    """单次修复的结果。"""
    ok: bool                      # 修复后是否为合法 JSON
    text: str                     # 修复后的文本（失败时为原文）
    repairs: list = field(default_factory=list)  # 触发的修复项（中文名）
    truncated: bool = False       # 是否触发了截断补全（best-effort）
    error: str = ""               # 失败时的原因


def _is_wordch(ch: str) -> bool:
    return bool(ch) and bool(_WORDCH_RE.match(ch))


# ---------------------------------------------------------- 字符串边界扫描
def _string_end(s: str, i: int):
    """s[i] 是一个引号字符。返回配对闭引号的下标；未闭合返回 None。

    严格配对：直引号只认直引号，智能开引号只认智能闭引号。
    这样智能引号出现在直引号字符串*内容*里时不会提前截断。
    单引号家族里，前后都是单词字符的引号视为撇号（it's），不算闭合。
    """
    n = len(s)
    q = s[i]
    if q == '"':
        closers = '"'
    elif q == "\u201c":
        closers = "\u201d"
    elif q == "\u201d":
        closers = "\u201c\u201d"
    elif q == "'":
        closers = "'"
    elif q == "\u2018":
        closers = "\u2019"
    else:  # '\u2019'
        closers = "\u2018\u2019"
    single_family = q in _SQ
    j = i + 1
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c in closers:
            if single_family:
                prev = s[j - 1] if j > 0 else ""
                nxt = s[j + 1] if j + 1 < n else ""
                if _is_wordch(prev) and _is_wordch(nxt):
                    j += 1  # 撇号，继续找
                    continue
            return j
        j += 1
    return None


def _find_first_opener(s: str):
    """字符串感知：找第一个不在字符串里的 { 或 [。"""
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in _DQ or c in _SQ:
            e = _string_end(s, i)
            i = (e + 1) if e is not None else n
        elif c == "{" or c == "[":
            return i
        else:
            i += 1
    return None


def _find_last_closer(s: str):
    """字符串感知：找最后一个不在字符串里的 } 或 ]。"""
    last = None
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in _DQ or c in _SQ:
            e = _string_end(s, i)
            i = (e + 1) if e is not None else n
        else:
            if c == "}" or c == "]":
                last = i
            i += 1
    return last


# ---------------------------------------------------------- 修复 8a：控制字符
def remove_control_chars(s: str):
    """删掉 JSON 里非法的裸控制字符（保留 \\t \\n \\r）。"""
    return _CTRL_RE.sub("", s)


# ---------------------------------------------------------- 修复 1：围栏与杂文
def _extract_scalar(s2: str, fence_removed: bool):
    """无括号时：顶层标量（字符串/数字/true/false/null），找第一个能
    raw_decode 的位置做精确提取。"""
    dec = json.JSONDecoder()
    n = len(s2)
    i = 0
    while i < n:
        if s2[i].isspace():
            i += 1
            continue
        try:
            _, end = dec.raw_decode(s2, i)
            body = s2[i:end]
            removed = s2[:i] + s2[end:]
            return body, (fence_removed or bool(removed.strip()))
        except Exception:
            pass
        i += 1
    return s2, fence_removed


def strip_fences_prose(s: str):
    """去掉 ``` 围栏行，再提取其中的 JSON 值。

    策略：先试 json.raw_decode 做精确提取（自动甩掉尾巴上的杂文和多余
    括号，也覆盖顶层标量）；失败则兜底用"首括号到尾括号"（可能截断，
    交给截断修复）。只有真正去掉了非空白杂文（或围栏行）才上报。
    """
    lines = s.split("\n")
    kept = [ln for ln in lines if not _FENCE_LINE_RE.match(ln)]
    s2 = "\n".join(kept)
    fence_removed = (s2 != s)
    dec = json.JSONDecoder()

    start = _find_first_opener(s2)
    if start is None:
        return _extract_scalar(s2, fence_removed)

    # 完整解析优先
    try:
        _, end = dec.raw_decode(s2, start)
        body, tail = s2[start:end], s2[end:]
        tc, tail_had_comment = remove_comments(tail)
        if tc.strip() or not tail_had_comment:
            # 尾巴有实质内容（或只是空白）：当杂文丢掉
            removed = s2[:start] + tail
            return body, (fence_removed or bool(removed.strip()))
        # 尾巴只有注释：留给注释修复
        removed = s2[:start]
        return body + tail, (fence_removed or bool(removed.strip()))
    except Exception:
        pass

    # 兜底：首括号到尾括号。尾巴先过一遍注释移除再判断：
    # 去掉注释后还有实质内容且含结构字符 -> 疑似截断片段，保留；
    # 尾巴只有注释 -> 留给注释修复；其余当杂文丢掉。
    end = _find_last_closer(s2)
    if end is None or end < start:
        return s2, fence_removed
    head, body, tail = s2[:start], s2[start:end + 1], s2[end + 1:]
    tc, tail_had_comment = remove_comments(tail)
    if tc.strip():
        keep_tail = any(ch in tc for ch in "{[\"'\u201c\u201d\u2018\u2019")
    else:
        keep_tail = tail_had_comment
    s3 = body + (tail if keep_tail else "")
    removed = head + ("" if keep_tail else tail)
    return s3, (fence_removed or bool(removed.strip()))


# ---------------------------------------------------------- 修复 2：注释
def remove_comments(s: str):
    """移除字符串之外的 // 行注释和 /* */ 块注释。"""
    out = []
    i, n = 0, len(s)
    changed = False
    while i < n:
        c = s[i]
        if c in _DQ or c in _SQ:
            e = _string_end(s, i)
            if e is None:
                out.append(s[i:])
                break
            out.append(s[i:e + 1])
            i = e + 1
        elif c == "/" and i + 1 < n and s[i + 1] == "/":
            changed = True
            j = s.find("\n", i)
            if j == -1:
                i = n
            else:
                out.append("\n")
                i = j + 1
        elif c == "/" and i + 1 < n and s[i + 1] == "*":
            changed = True
            j = s.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out), changed


# ---------------------------------------------------------- 分词器
def _decode_string_content(body: str) -> str:
    """解码字符串内部的转义序列（尽力而为，未知转义原样保留）。"""
    out = []
    i, n = 0, len(body)
    while i < n:
        c = body[i]
        if c == "\\" and i + 1 < n:
            nx = body[i + 1]
            if nx == "u" and i + 5 < n:
                try:
                    out.append(chr(int(body[i + 2:i + 6], 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            if nx in _ESCAPES:
                out.append(_ESCAPES[nx])
            else:
                out.append("\\" + nx)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def tokenize(s: str):
    """把 JSON 文本切成 token：(kind, payload)。

    kind: 'string' | 'word' | 'number' | 'punct' | 'space' | 'other'
    string 的 payload 是 (open_quote, raw, decoded_content)。
    分词器不对畸形输入抛异常：未闭合的字符串会吞到行尾/EOF 为止。
    """
    toks = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c in _DQ or c in _SQ:
            e = _string_end(s, i)
            if e is None:
                raw = s[i:]
                toks.append(("string", (c, raw, _decode_string_content(s[i + 1:]))))
                i = n
            else:
                raw = s[i:e + 1]
                toks.append(("string", (c, raw, _decode_string_content(s[i + 1:e]))))
                i = e + 1
        elif c.isspace():
            j = i
            while j < n and s[j].isspace():
                j += 1
            toks.append(("space", s[i:j]))
            i = j
        elif c in "{}[],:":
            toks.append(("punct", c))
            i += 1
        elif c == "-" or c.isdigit() or (c == "." and i + 1 < n and s[i + 1].isdigit()):
            m = _NUM_RE.match(s, i)
            if m:
                toks.append(("number", m.group(0)))
                i = m.end()
            else:
                toks.append(("other", c))
                i += 1
        elif _is_wordch(c) and not c.isdigit() or c == "$":
            m = _WORD_RE.match(s, i)
            toks.append(("word", m.group(0)))
            i = m.end()
        else:
            toks.append(("other", c))
            i += 1
    return toks


def render_token(tok) -> str:
    """把 token 还原为原文（string 用 raw，保证分词前后文本一致）。"""
    kind, payload = tok
    if kind == "string":
        return payload[1]
    return payload

# ---------------------------------------------------------- 修复 3/4/5/6/8b：重建
def rebuild(toks):
    """基于 token 重建文本，顺手做以下修复：

    - 多余逗号（, 后面紧跟 } / ]，或行尾）：删掉
    - 单引号（及智能引号）定界的字符串：转成标准双引号
    - 未加引号的键名：在对象上下文里补上引号
    - True/False/None（非键名位置）：转成小写
    - 字符串里的裸换行/制表符：转成转义（属"控制字符规范化"）
    - 智能引号作定界符：统一成直引号（属"智能引号规范化"）

    返回 (new_text, fired_repairs)。
    合法 JSON 走这里会原样返回（幂等）。
    """
    out = []
    fired = set()
    # 非空白 token 序列，用于向前看（多余逗号判断）；用 id 定位避免歧义
    sig = [t for t in toks if t[0] != "space"]
    sig_pos_of = {}
    for i, t in enumerate(toks):
        if t[0] != "space":
            sig_pos_of[i] = len(sig_pos_of)

    stack = []            # '{' / '[' 栈
    prev_kind = None      # 上一个有效 token 的 kind
    prev_text = None      # 上一个有效 token 的文本（punct/word/number）

    for i, (kind, payload) in enumerate(toks):
        if kind == "space" or kind == "other":
            out.append(payload if kind == "space" else payload)
            continue
        if kind == "punct" and payload == ",":
            # 向前看：下一个有效 token 是 } / ]，或是已经到头 -> 多余逗号
            p = sig_pos_of[i]
            nxt = sig[p + 1] if p + 1 < len(sig) else None
            if nxt is None or (nxt[0] == "punct" and nxt[1] in "}]"):
                fired.add(R_COMMA)
                continue
            out.append(",")
            prev_kind, prev_text = "punct", ","
            continue
        if kind == "punct" and payload in "{[":
            stack.append(payload)
            out.append(payload)
            prev_kind, prev_text = "punct", payload
            continue
        if kind == "punct" and payload in "}]":
            if stack:
                stack.pop()
            out.append(payload)
            prev_kind, prev_text = "punct", payload
            continue
        if kind == "punct":  # ':' 或其它标点，原样保留
            out.append(payload)
            prev_kind, prev_text = "punct", payload
            continue
        if kind == "string":
            q, raw, content = payload
            needs_rebuild = (
                q in _SQ
                or q in "\u201c\u201d"
                or "\n" in raw
                or "\r" in raw
                or "\t" in raw
            )
            if needs_rebuild:
                out.append(json.dumps(content, ensure_ascii=False))
                if q in _SQ:
                    fired.add(R_SQUOTE)
                if q in "\u201c\u201d":
                    fired.add(R_CLEAN)
                if q == '"' and (("\n" in raw) or ("\r" in raw) or ("\t" in raw)):
                    fired.add(R_CLEAN)
            else:
                out.append(raw)
            prev_kind, prev_text = "string", None
            continue
        if kind == "word":
            is_key = (
                stack
                and stack[-1] == "{"
                and prev_kind == "punct"
                and prev_text in "{,"
            )
            if is_key:
                # 键名位置：先加引号，True 之类作键名也不转换字面量
                out.append(json.dumps(payload, ensure_ascii=False))
                fired.add(R_KEY)
            elif payload == "True":
                out.append("true")
                fired.add(R_LITERAL)
            elif payload == "False":
                out.append("false")
                fired.add(R_LITERAL)
            elif payload == "None":
                out.append("null")
                fired.add(R_LITERAL)
            else:
                out.append(payload)
            prev_kind, prev_text = "word", payload
            continue
        if kind == "number":
            out.append(payload)
            prev_kind, prev_text = "number", payload
            continue
    return "".join(out), fired


# ---------------------------------------------------------- 修复 7：截断补全
def _close_brackets(s: str) -> str:
    """补上未闭合的引号和括号（best-effort）。"""
    stack = []
    i, n = 0, len(s)
    in_str = False
    while i < n:
        c = s[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                stack.append("}")
            elif c == "[":
                stack.append("]")
            elif c in "}]":
                if stack and stack[-1] == c:
                    stack.pop()
        i += 1
    t = s
    if in_str:
        t += '"'
    t += "".join(reversed(stack))
    return t


def repair_truncation(s: str):
    """尝试若干补全候选，返回 (fixed, ok)。

    候选顺序：直接补全 -> 去掉行尾半截字面量/数字再补全
    -> 删掉悬空的 "key": 再补全。第一个能 json.loads 的胜出。
    """
    t = s.rstrip()
    candidates = [_close_brackets(t)]

    t2 = _PARTIAL_WORD_RE.sub("", t).rstrip()
    t2 = _PARTIAL_NUM_RE.sub("", t2).rstrip()
    if t2 != t:
        candidates.append(_close_brackets(t2))

    t3 = _DANGLING_KV_RE.sub(r"\1", t2).rstrip().rstrip(",").rstrip()
    if t3 != t2:
        candidates.append(_close_brackets(t3))

    for cand in candidates:
        try:
            json.loads(cand)
            return cand, True
        except Exception:
            continue
    return s, False


# ---------------------------------------------------------- 总装
def fix_json(text: str) -> FixResult:
    """对一段文本做全套修复，返回 FixResult。"""
    seen = set()

    t = remove_control_chars(text)
    if t != text:
        seen.add(R_CLEAN)

    t2, changed = strip_fences_prose(t)
    if changed:
        seen.add(R_FENCE)
    t = t2

    t2, changed = remove_comments(t)
    if changed:
        seen.add(R_COMMENT)
    t = t2

    t2, fired = rebuild(tokenize(t))
    seen |= fired
    t = t2

    try:
        json.loads(t)
        repairs = [r for r in _ORDER if r in seen]
        return FixResult(ok=True, text=t, repairs=repairs)
    except Exception as e:
        parse_error = str(e)

    fixed, ok = repair_truncation(t)
    if ok:
        seen.add(R_TRUNC)
        repairs = [r for r in _ORDER if r in seen]
        return FixResult(ok=True, text=fixed, repairs=repairs, truncated=True)

    repairs = [r for r in _ORDER if r in seen]
    return FixResult(ok=False, text=text, repairs=repairs, error=parse_error)


# ---------------------------------------------------------- CLI
def _read_input(path):
    if path:
        with open(path, encoding="utf-8-sig") as f:
            return f.read()
    if sys.stdin.isatty():
        return None
    data = sys.stdin.read()
    return data.lstrip("\ufeff")


def _write_output(path, text):
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)


def _cmd_check(lines, whole):
    """只校验不修复。返回 exit code。"""
    if whole is not None:
        try:
            json.loads(whole)
            print("校验通过：整文件是合法 JSON", file=sys.stderr)
            return 0
        except Exception as e:
            print(f"校验失败：整文件不是合法 JSON：{e}", file=sys.stderr)
            return 1
    bad = []
    total = 0
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            continue
        total += 1
        try:
            json.loads(line)
        except Exception as e:
            bad.append((lineno, str(e)))
    if bad:
        for lineno, err in bad:
            print(f"第{lineno}行无效：{err}", file=sys.stderr)
        print(f"校验失败：{total} 行中有 {len(bad)} 行无效", file=sys.stderr)
        return 1
    print(f"校验通过：{total} 行全部有效", file=sys.stderr)
    return 0


def _cmd_jsonl(lines, args):
    out_lines = []
    n_total = n_fixed = n_bad = 0
    for lineno, line in enumerate(lines, 1):
        if not line.strip():
            out_lines.append(line)
            continue
        n_total += 1
        r = fix_json(line)
        if r.ok:
            out_lines.append(r.text)
            if r.repairs:
                n_fixed += 1
                if args.diff:
                    tag = "（内容截断，仅补全结构）" if r.truncated else ""
                    print(f"--- 第{lineno}行 [{', '.join(r.repairs)}]{tag}", file=sys.stderr)
                    print(f"- {line}", file=sys.stderr)
                    print(f"+ {r.text}", file=sys.stderr)
        else:
            n_bad += 1
            out_lines.append(line)  # 修不好也保留原行，不丢数据
            print(f"第{lineno}行无法修复：{r.error}", file=sys.stderr)
    _write_output(args.output, "\n".join(out_lines))
    print(
        f"处理 {n_total} 行：{n_fixed} 行已修复，{n_bad} 行无法修复",
        file=sys.stderr,
    )
    return 0 if n_bad == 0 else 1


def _cmd_whole(data, args):
    r = fix_json(data)
    if args.diff and r.repairs:
        tag = "（内容截断，仅补全结构）" if r.truncated else ""
        print(f"--- 整文件 [{', '.join(r.repairs)}]{tag}", file=sys.stderr)
        print(f"- {data[:2000]}", file=sys.stderr)
        print(f"+ {r.text[:2000]}", file=sys.stderr)
    _write_output(args.output, r.text if r.ok else data)
    if r.ok:
        if r.repairs:
            print(f"修复完成：{', '.join(r.repairs)}", file=sys.stderr)
        else:
            print("无需修复：已是合法 JSON", file=sys.stderr)
        return 0
    print(f"无法修复：{r.error}", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="jsonlfix",
        description="修复 LLM 输出的残缺 JSON / JSONL（标准库、零依赖）",
    )
    ap.add_argument("input", nargs="?", help="输入文件；省略则从 stdin 读取")
    ap.add_argument("-o", "--output", help="输出文件；省略则写到 stdout")
    ap.add_argument("--json", action="store_true",
                    help="整文件视为单个 JSON（默认按 JSONL 逐行处理）")
    ap.add_argument("--check", action="store_true",
                    help="只校验不修复；有无效内容则 exit 1，并列出行号与原因")
    ap.add_argument("--diff", action="store_true",
                    help="在 stderr 显示每处修改的前后对比")
    args = ap.parse_args(argv)

    try:
        data = _read_input(args.input)
    except OSError as e:
        print(f"error: 无法读取输入文件：{e}", file=sys.stderr)
        return 2
    if data is None:
        ap.print_usage(sys.stderr)
        print("error: 请指定输入文件，或通过管道从 stdin 输入", file=sys.stderr)
        return 2

    if args.check:
        if args.json:
            return _cmd_check(None, data)
        return _cmd_check(data.split("\n"), None)
    if args.json:
        return _cmd_whole(data, args)
    return _cmd_jsonl(data.split("\n"), args)


if __name__ == "__main__":
    raise SystemExit(main())
