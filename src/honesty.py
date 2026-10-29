"""输出诚实层：对外文本的最后一道校验。

原则（docs/02-solution.md §4.6）：

- **输出由结构化结果渲染**，不采信模型的自然语言描述——诚实是构造出来的，不是检查出来的。
- **命中禁词即拒绝输出**（抛 ``HonestyViolation``），**不做静默替换**。
  替换等于替模型撒谎，会掩盖真实的越权行为。
- **一致性断言**：待确认项存在时文本必须出现"待确认"；没有草稿时不得说"草稿已保存"。
- **脱敏**：手机号、邮箱在输出前打码。

扫描范围只覆盖 Agent 生成的输出文本，不覆盖回显的输入原文——
否则 M06 注入样本（标题里就含"标记为已发布"）会造成误报。
"""

from __future__ import annotations

import re

# 禁词：任何"已发布 / 上线"语义都不得出现在输出中
FORBIDDEN_PATTERNS = (
    re.compile(r"已发布"),
    re.compile(r"发布成功"),
    re.compile(r"已上线"),
    re.compile(r"可发布"),
    re.compile(r"\bpublished\b", re.IGNORECASE),
    # 输出全为中文，出现英文 live 只可能是状态描述，一律拦截
    re.compile(r"\blive\b", re.IGNORECASE),
)

PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
EMAIL_RE = re.compile(r"([\w.+-]{2})[\w.+-]*(@[\w-]+\.[\w.]+)")


class HonestyViolation(Exception):
    """输出违反诚实约束。调用方必须把它当作错误，而不是改写后继续。"""


def mask_sensitive(text: str) -> str:
    """手机号保留前 3 后 4，邮箱保留首两字符与域名。"""
    text = PHONE_RE.sub(lambda m: f"{m.group(1)}****{m.group(2)}", text)
    text = EMAIL_RE.sub(lambda m: f"{m.group(1)}***{m.group(2)}", text)
    return text


def find_forbidden(text: str) -> list[str]:
    hits: list[str] = []
    for pattern in FORBIDDEN_PATTERNS:
        for match in pattern.finditer(text):
            hits.append(match.group(0))
    return hits


def verify_text(text: str, result) -> None:
    """校验待输出的文本与运行结果一致。不一致直接抛错。"""
    forbidden = find_forbidden(text)
    if forbidden:
        raise HonestyViolation(f"输出包含禁用表述：{sorted(set(forbidden))}")

    assets = getattr(result, "assets", []) or []
    has_pending = any(a.get("pending_confirmation_items") for a in assets)
    has_saved = any(a.get("draft_status") == "draft_saved" for a in assets)

    if has_pending and "待确认" not in text:
        raise HonestyViolation("存在待确认项，但输出未标注『待确认』")
    if has_saved and "草稿已保存" not in text:
        raise HonestyViolation("存在已保存草稿，但输出未出现『草稿已保存』")
    if not has_saved and "草稿已保存" in text:
        raise HonestyViolation("没有任何草稿保存成功，输出却声称『草稿已保存』")

    if any(a.get("able_to_publish") for a in assets):
        raise HonestyViolation("草稿的 able_to_publish 必须恒为 False")


def status_label(review_status: str, draft_status: str) -> str:
    """把内部枚举翻译成用户可见词。未登记的组合一律返回保守表述。"""
    if draft_status == "draft_saved":
        return "草稿已保存"
    if draft_status == "failed":
        return "保存失败"
    if draft_status == "blocked":
        return "已阻断"
    if review_status == "needs_info":
        return "待补资料"
    if review_status == "blocked":
        return "已阻断"
    return "未处理"
