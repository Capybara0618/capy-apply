"""BOSS message normalization for Capybot Apply."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import normalize_utc_iso

HUMAN_MESSAGE_TYPES = {"text", "image", "file"}
NON_HUMAN_MESSAGE_TYPES = {"job_card", "platform_card", "auto_followup", "system"}


class BossMessageNormalizer:
    """Convert raw BOSS history messages into stable local message records."""

    AUTO_FOLLOWUP_MARKERS = [
        "VIP求职助手帮你追聊",
        "求职助手帮你追聊",
        "BOSS求职助手",
        "我对这个岗位很感兴趣，期待您的回复",
        "期待您的回复~",
        "期待您的回复～",
    ]
    SYSTEM_MARKERS = [
        "系统消息",
        "安全提示",
        "平台提醒",
        "为保护求职招聘双方的信息安全",
        "请打码后再发",
        "存在联系方式",
    ]
    PLATFORM_CARD_MARKERS = [
        "点击修改打招呼语",
        "去修改打招呼语",
        "我是你的求职助手",
    ]
    RESUME_READ_BIZ_TYPES = {21050035, "21050035"}
    RESUME_SENT_MARKERS = ["附件简历", "已发送给Boss", "已发送给 BOSS"]

    @classmethod
    def normalize(
        cls,
        conversation_id: str,
        raw: dict[str, Any],
        *,
        import_run_id: str | None = None,
        identity_hints: dict[str, str | None] | None = None,
    ) -> dict[str, Any]:
        sender = raw.get("from") if isinstance(raw.get("from"), dict) else {}
        text = cls.extract_text(raw)
        message_type = cls.classify(raw)
        attachment_meta = cls.attachment_meta(raw, message_type)
        from_me, confidence = cls.infer_from_me(
            raw,
            text=text,
            message_type=message_type,
            identity_hints=identity_hints,
        )
        sender_uid = str(sender.get("uid") or raw.get("sender_uid") or "")
        sender_name = sender.get("name") or raw.get("sender_name")
        if from_me and cls._is_resume_sent_action(raw, text):
            sender_uid = str((identity_hints or {}).get("candidate_uid") or sender_uid)
            sender_name = (identity_hints or {}).get("candidate_name") or sender_name
        content_fingerprint = cls.content_fingerprint(
            raw, text=text, message_type=message_type, attachment_meta=attachment_meta
        )
        message_id = str(raw.get("mid") or raw.get("message_id") or "")
        if not message_id:
            message_id = f"fp:{content_fingerprint}"
        return {
            "platform": "boss",
            "conversation_id": conversation_id,
            "message_id": message_id,
            "from_me": from_me,
            "from_me_confidence": confidence,
            "sender_uid": sender_uid,
            "sender_name": sender_name,
            "sender_role": "system" if message_type == "system" else raw.get("sender_role"),
            "text": text,
            "message_type": message_type,
            "is_human_message": 1 if message_type in HUMAN_MESSAGE_TYPES else 0,
            "attachment_meta": attachment_meta,
            "content_fingerprint": content_fingerprint,
            "first_seen_import_run_id": import_run_id,
            "sent_at": normalize_utc_iso(raw.get("sent_at") or raw.get("time")),
            "raw_payload": raw,
        }

    @classmethod
    def classify(cls, raw: dict[str, Any]) -> str:
        body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
        text = cls.extract_text(raw)
        compact = text.strip().lower() if isinstance(text, str) else ""
        if body.get("jobDesc") or body.get("type") == 8:
            return "job_card"
        if cls._is_resume_read_receipt(raw, text):
            return "system"
        if text and any(marker in text for marker in cls.PLATFORM_CARD_MARKERS):
            return "platform_card"
        dialog = body.get("dialog") if isinstance(body.get("dialog"), dict) else {}
        if isinstance(dialog.get("text"), str) and dialog.get("text", "").strip():
            return "text"
        if body.get("image") or body.get("type") == 3:
            return "image"
        if cls._is_resume_sent_action(raw, text):
            return "file"
        if (
            body.get("file")
            or body.get("resume")
            or body.get("attachment")
            or body.get("type") in {5, 6, 7}
        ):
            return "file"
        if body.get("articles") or body.get("article") or body.get("type") in {16, 17, 18}:
            return "platform_card"
        if text and any(marker in text for marker in cls.AUTO_FOLLOWUP_MARKERS):
            return "auto_followup"
        if compact in {"system", "[system]"}:
            return "system"
        if text and any(marker in text for marker in cls.SYSTEM_MARKERS):
            return "system"
        if text:
            return "text"
        if body:
            return "unknown"
        return "unknown"

    @classmethod
    def extract_text(cls, raw: dict[str, Any]) -> str | None:
        body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
        text = body.get("text") or raw.get("text")
        if isinstance(text, str) and text.strip():
            return text
        dialog = body.get("dialog") if isinstance(body.get("dialog"), dict) else {}
        dialog_text = dialog.get("text")
        if isinstance(dialog_text, str) and dialog_text.strip():
            return dialog_text
        hyperlink = body.get("hyperLink") if isinstance(body.get("hyperLink"), dict) else {}
        hyperlink_text = hyperlink.get("text")
        if isinstance(hyperlink_text, str) and hyperlink_text.strip():
            return hyperlink_text
        job = body.get("jobDesc")
        if isinstance(job, dict):
            parts = [job.get("title"), job.get("company"), job.get("salary"), job.get("city")]
            return "岗位卡：" + " ".join(str(p) for p in parts if p)
        articles = body.get("articles")
        if isinstance(articles, list) and articles:
            first = articles[0] if isinstance(articles[0], dict) else {}
            parts = [first.get("title"), first.get("subTitle"), first.get("description")]
            joined = " ".join(str(p) for p in parts if p)
            return joined or None
        image = body.get("image")
        if isinstance(image, dict):
            return "[图片/附件]"
        file_info = body.get("file") or body.get("resume") or body.get("attachment")
        if isinstance(file_info, dict):
            name = file_info.get("name") or file_info.get("fileName") or file_info.get("title")
            return f"[文件/简历附件]{' ' + str(name) if name else ''}"
        return None

    @classmethod
    def attachment_meta(cls, raw: dict[str, Any], message_type: str) -> dict[str, Any]:
        body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
        if message_type == "image":
            image = body.get("image") if isinstance(body.get("image"), dict) else {}
            origin = image.get("originImage") if isinstance(image.get("originImage"), dict) else {}
            tiny = image.get("tinyImage") if isinstance(image.get("tinyImage"), dict) else {}
            return {
                "kind": "image",
                "url": origin.get("url") or tiny.get("url"),
                "width": origin.get("width") or tiny.get("width"),
                "height": origin.get("height") or tiny.get("height"),
            }
        if message_type == "file":
            file_info = body.get("file") or body.get("resume") or body.get("attachment") or {}
            hyperlink = body.get("hyperLink") if isinstance(body.get("hyperLink"), dict) else {}
            return {
                "kind": "file",
                "name": file_info.get("name")
                or file_info.get("fileName")
                or file_info.get("title")
                or hyperlink.get("text"),
                "url": file_info.get("url") or file_info.get("downloadUrl") or hyperlink.get("url"),
            }
        if message_type == "platform_card":
            return {
                "kind": "platform_card",
                "body_type": body.get("type"),
                "style": body.get("style"),
            }
        return {}

    @classmethod
    def infer_from_me(
        cls,
        raw: dict[str, Any],
        *,
        text: str | None = None,
        message_type: str | None = None,
        identity_hints: dict[str, str | None] | None = None,
    ) -> tuple[bool, float]:
        if "from_me" in raw:
            return bool(raw.get("from_me")), 0.95
        hints = identity_hints or {}
        sender = raw.get("from") if isinstance(raw.get("from"), dict) else {}
        sender_uid = str(sender.get("uid") or raw.get("sender_uid") or "")
        sender_name = str(sender.get("name") or raw.get("sender_name") or "")
        candidate_uid = str(hints.get("candidate_uid") or "")
        recruiter_uid = str(hints.get("recruiter_uid") or "")
        candidate_name = str(hints.get("candidate_name") or "")
        recruiter_name = str(hints.get("recruiter_name") or "")
        if cls._is_resume_sent_action(raw, text):
            return True, 0.92
        if cls._is_resume_read_receipt(raw, text):
            return False, 0.98
        if sender_uid and candidate_uid and sender_uid == candidate_uid:
            return True, 0.99
        if sender_uid and recruiter_uid and sender_uid == recruiter_uid:
            return False, 0.99
        if sender_name and candidate_name and sender_name == candidate_name:
            return True, 0.9
        if sender_name and recruiter_name and sender_name == recruiter_name:
            return False, 0.9
        # status=2 and received=true occur on both sides in BOSS history and
        # must not be treated as recruiter identity. status=1 is retained only
        # as a weak compatibility signal for older payloads.
        status = raw.get("status")
        if status in {1, "1"}:
            return True, 0.65
        if isinstance(text, str) and any(
            marker in text
            for marker in [
                "您好，可以占用您一点时间",
                "看到贵司",
                "我目前",
                "我的简历",
                "可连续实习",
                "期待您的回复",
                "我对这个岗位很感兴趣",
            ]
        ):
            return True, 0.75
        if "received" in raw:
            return (not bool(raw.get("received"))), 0.4
        if message_type in NON_HUMAN_MESSAGE_TYPES:
            return False, 0.5
        return False, 0.35

    @classmethod
    def identity_hints(cls, messages: list[dict[str, Any]]) -> dict[str, str | None]:
        """Infer stable candidate/recruiter identities from a conversation batch."""

        hints: dict[str, str | None] = {
            "candidate_uid": None,
            "candidate_name": None,
            "recruiter_uid": None,
            "recruiter_name": None,
        }
        for raw in messages:
            body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
            job = body.get("jobDesc") if isinstance(body.get("jobDesc"), dict) else {}
            geek = job.get("geek") if isinstance(job.get("geek"), dict) else {}
            boss = job.get("boss") if isinstance(job.get("boss"), dict) else {}
            if geek:
                hints["candidate_uid"] = str(geek.get("uid") or "") or hints["candidate_uid"]
                hints["candidate_name"] = str(geek.get("name") or "") or hints["candidate_name"]
            if boss:
                hints["recruiter_uid"] = str(boss.get("uid") or "") or hints["recruiter_uid"]
                hints["recruiter_name"] = str(boss.get("name") or "") or hints["recruiter_name"]
        for raw in messages:
            sender = raw.get("from") if isinstance(raw.get("from"), dict) else {}
            sender_uid = str(sender.get("uid") or "")
            sender_name = str(sender.get("name") or "")
            if sender_uid and sender_uid == hints["candidate_uid"] and sender_name:
                hints["candidate_name"] = hints["candidate_name"] or sender_name
            if sender_uid and sender_uid == hints["recruiter_uid"] and sender_name:
                hints["recruiter_name"] = hints["recruiter_name"] or sender_name
        if hints["candidate_uid"] and hints["candidate_name"]:
            return hints
        for raw in messages:
            text = cls.extract_text(raw)
            if not cls._looks_like_candidate_intro(text):
                continue
            sender = raw.get("from") if isinstance(raw.get("from"), dict) else {}
            hints["candidate_uid"] = str(sender.get("uid") or "") or hints["candidate_uid"]
            hints["candidate_name"] = str(sender.get("name") or "") or hints["candidate_name"]
            break
        return hints

    @staticmethod
    def _looks_like_candidate_intro(text: str | None) -> bool:
        if not isinstance(text, str):
            return False
        return any(
            marker in text
            for marker in [
                "看到贵司",
                "我目前",
                "可连续实习",
                "我的简历",
                "我对这个岗位很感兴趣",
            ]
        )

    @classmethod
    def _is_resume_read_receipt(cls, raw: dict[str, Any], text: str | None) -> bool:
        if raw.get("bizType") in cls.RESUME_READ_BIZ_TYPES:
            return True
        return isinstance(text, str) and "对方已查看了您的附件简历" in text

    @classmethod
    def _is_resume_sent_action(cls, raw: dict[str, Any], text: str | None) -> bool:
        body = raw.get("body") if isinstance(raw.get("body"), dict) else {}
        if not isinstance(body.get("hyperLink"), dict):
            return False
        return isinstance(text, str) and all(
            marker in text for marker in cls.RESUME_SENT_MARKERS[:2]
        )

    @staticmethod
    def content_fingerprint(
        raw: dict[str, Any], *, text: str | None, message_type: str, attachment_meta: dict[str, Any]
    ) -> str:
        sender = raw.get("from") if isinstance(raw.get("from"), dict) else {}
        payload = {
            "sender": sender.get("uid")
            or sender.get("name")
            or raw.get("sender_uid")
            or raw.get("sender_name"),
            "time": raw.get("sent_at") or raw.get("time"),
            "type": message_type,
            "text": text or "",
            "attachment": attachment_meta,
        }
        return hashlib.sha1(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
