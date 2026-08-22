"""Small deterministic signals that protect, but do not replace, semantic analysis."""

from __future__ import annotations

import re


class ConversationSignals:
    @staticmethod
    def is_platform_assistant_conversation(*, contact_name: str, preview: str = "") -> bool:
        """Identify BOSS platform-assistant threads without matching HR auto-followups."""

        name = contact_name.strip().lower()
        text = preview.strip().lower()
        return "求职助手" in name or any(
            marker in text
            for marker in (
                "您正在与boss求职助手沟通",
                "我是你的求职助手",
            )
        )

    @staticmethod
    def requires_reply(text: str) -> bool:
        value = text.strip()
        if not value:
            return False
        return bool(
            re.search(
                r"[?？]|方便|是否|可否|能否|有兴趣|什么时候|多久|接受吗|"
                r"发一份|过来|参加",
                value,
            )
        )

    @staticmethod
    def is_ambiguous_invitation(text: str) -> bool:
        return bool(re.search(r"沙龙|活动|交流会|宣讲会|分享会|过来听听", text))

    @staticmethod
    def is_interview_invitation(text: str) -> bool:
        if re.search(
            r"不是面试|不代表.{0,6}面试|后续.{0,10}(是否|能否|再).{0,6}面试|"
            r"是否进入面试.{0,8}(评估|确认)|面试.{0,8}(需要|还需).{0,6}评估",
            text,
        ):
            return False
        return bool(
            re.search(
                r"面试邀请|参加.{0,12}面试|安排.{0,12}面试|"
                r"约.{0,10}(线上|视频|电话|面试|沟通)|"
                r"线上面试|视频面试|电话面试|面试时间|会议链接|几点方便",
                text,
            )
        )

    @staticmethod
    def asks_for_job_context(text: str) -> bool:
        return bool(
            re.search(
                r"(?:你|候选人).{0,12}(?:项目|技术).{0,8}(?:经历|经验|工作|贡献)|"
                r"(?:介绍|说明).{0,8}(?:你|做过|负责).{0,12}(?:项目|技术)|"
                r"岗位要求|岗位职责|薪资范围|面试准备",
                text,
            )
        )

    @staticmethod
    def requests_candidate_profile(text: str) -> bool:
        """Detect when a grounded reply needs resume/profile evidence."""

        return bool(
            re.search(
                r"自我介绍|介绍.{0,10}(?:一下|下)?自己|"
                r"(?:介绍|说说|聊聊).{0,12}(?:项目|技术栈|经历|经验)|"
                r"(?:项目|技术).{0,10}(?:经历|经验|做过|负责|贡献)|"
                r"最近.{0,8}(?:做的|参与的)?项目",
                text,
            )
        )

    @staticmethod
    def is_material_request(text: str) -> bool:
        return bool(
            re.search(
                r"(?:发|提供|上传|补充|同意).{0,10}(?:简历|作品集|附件|项目材料)|"
                r"(?:简历|作品集|附件|项目材料).{0,10}(?:发|提供|上传|补充|同意)",
                text,
            )
        )

    @staticmethod
    def is_critical_safety_risk(text: str) -> bool:
        """Detect explicit payment risk only; broader risk semantics stay with the LLM."""

        return bool(
            re.search(
                r"培训贷|培训费|入职费|报名费|押金|保证金|先交.{0,6}(钱|费)|"
                r"缴纳.{0,6}(费用|款项)|费用.{0,8}(分期|贷款)",
                text,
            )
        )
