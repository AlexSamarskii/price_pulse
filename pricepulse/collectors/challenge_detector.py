from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ChallengeType(str, Enum):
    NONE = "none"
    CLOUDFLARE_TURNSTILE = "cloudflare_turnstile"
    GEETEST_SLIDER = "geetest_slider"
    NETEASE_DUN = "netease_dun"
    RECAPTCHA = "recaptcha"
    HCAPTCHA = "hcaptcha"
    TENCENT_CAPTCHA = "tencent_captcha"
    GENERIC_BLOCK = "generic_block"


@dataclass(frozen=True)
class CaptchaChallenge:
    challenge_type: ChallengeType
    title: str
    target_selector: str
    details: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge_type": self.challenge_type.value,
            "title": self.title,
            "target_selector": self.target_selector,
            "details": self.details,
        }


@dataclass(frozen=True)
class _ChallengeRule:
    challenge_type: ChallengeType
    patterns: tuple[str, ...]
    target_selector: str
    details: str


class CaptchaDetector:
    RULES = (
        _ChallengeRule(
            ChallengeType.CLOUDFLARE_TURNSTILE,
            (
                r"challenges\.cloudflare\.com",
                r"cf-turnstile",
                r"Just a moment\.\.\.",
                r"_cf_chl_opt",
                r"turnstile-wrapper",
            ),
            'iframe[src*="challenges.cloudflare.com"], .cf-turnstile',
            "Cloudflare Turnstile or Managed Challenge detected",
        ),
        _ChallengeRule(
            ChallengeType.GEETEST_SLIDER,
            (r"geetest", r"gt_slider", r"geetest_holder", r"geetest_radar_tip"),
            ".geetest_holder, .geetest_radar_tip, .geetest_slider",
            "Geetest slider or click challenge detected",
        ),
        _ChallengeRule(
            ChallengeType.NETEASE_DUN,
            (
                r"cstaticdun",
                r"dun\.163\.com",
                r"NECaptcha",
                r"yidun_input",
                r"yidun_popup",
                r"yidun_modal",
                r"NECaptchaValidate",
                "\u7f51\u6613\u6613\u76fe",
                r"\byidun\b",
            ),
            ".yidun_popup, .yidun_modal, input[name='NECaptchaValidate'], .yidun_slider, .yidun",
            "NetEase Dun slider or puzzle challenge detected",
        ),
        _ChallengeRule(
            ChallengeType.RECAPTCHA,
            (r"google\.com/recaptcha", r"g-recaptcha", r"recaptcha/api\.js"),
            'iframe[src*="google.com/recaptcha"], .g-recaptcha',
            "Google reCAPTCHA challenge detected",
        ),
        _ChallengeRule(
            ChallengeType.HCAPTCHA,
            (r"hcaptcha\.com", r"h-captcha"),
            'iframe[src*="hcaptcha.com"], .h-captcha',
            "hCaptcha challenge detected",
        ),
        _ChallengeRule(
            ChallengeType.TENCENT_CAPTCHA,
            (r"captcha\.gtimg\.com", r"TencentCaptcha", r"turing\.captcha"),
            "#TCaptcha, .tcaptcha-transform",
            "Tencent security challenge detected",
        ),
        _ChallengeRule(
            ChallengeType.GENERIC_BLOCK,
            (
                "\u5b89\u5168\u9a8c\u8bc1",
                "\u8bf7\u5b8c\u6210\u5b89\u5168\u9a8c\u8bc1",
                "\u5b8c\u6210\u5b89\u5168\u9a8c\u8bc1",
                "\u4eba\u673a\u9a8c\u8bc1",
                "\u9a8c\u8bc1\u7801",
                r"Security Check",
                r"Access Denied",
                r"Attention Required! \| Cloudflare",
                r"Verify you are human",
                r"Robot or Human",
            ),
            "body",
            "Generic anti-bot security barrier detected",
        ),
    )

    def detect_html(self, html: str, title: str = "") -> CaptchaChallenge | None:
        combined = f"{title}\n{html}"
        for rule in self.RULES:
            if any(re.search(pattern, combined, re.IGNORECASE) for pattern in rule.patterns):
                return CaptchaChallenge(
                    challenge_type=rule.challenge_type,
                    title=title,
                    target_selector=rule.target_selector,
                    details=rule.details,
                )
        return None
