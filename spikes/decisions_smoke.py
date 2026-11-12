"""Run one Decisions Choice request without sending project data."""

import argparse
import json
import math
import os
import socket
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def classify_answer(payload, candidates):
    answers = payload.get("answers") if isinstance(payload, dict) else None
    if (not isinstance(answers, list) or len(answers) != 1 or not isinstance(answers[0], dict)
            or answers[0].get("name") != "next_action"):
        return "DECISION_INVALID", None
    answer = answers[0]
    if answer.get("type") == "refusal":
        return "DECISION_REFUSED", None
    choice = answer.get("choice")
    if answer.get("type") != "choice" or not isinstance(choice, str) or choice not in candidates:
        return "DECISION_INVALID", None
    return "OK", choice


def retry_after(headers):
    value = headers.get("Retry-After", "")
    if value.isascii() and value.isdecimal() and len(value) <= 9:
        return int(value)
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is not None:
            return max(0, (when - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        pass
    return None


def choice_body(state, choices):
    return json.dumps({
        "model": "gpt-6-luna",
        "input": state,
        "questions": [{
            "type": "choice",
            "name": "next_action",
            "instructions": "Choose one supplied value appropriate for the current QA state.",
            "choices": [{"value": value, "description": desc} for value, desc in choices.items()],
        }],
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def request_choice(state, choices, *, key=None, metadata=None, timeout=10):
    if not 2 <= len(choices) <= 255:
        return "DECISION_INVALID_CANDIDATES", None
    key = os.environ.get("OPENAI_API_KEY", "").strip() if key is None else key
    if not key:
        return "API_KEY_MISSING", None
    request = Request(
        "https://api.openai.com/v1/decisions",
        data=choice_body(state, choices),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with build_opener(RejectRedirects()).open(request, timeout=timeout) as response:
            result = json.load(response)
        status, choice = classify_answer(result, choices)
        usage = result.get("usage") if isinstance(result, dict) else None
        if metadata is not None and status == "OK":
            confidence = result["answers"][0].get("confidence")
            if type(confidence) in (int, float) and math.isfinite(confidence) and 0 <= confidence <= 1:
                metadata["confidence"] = confidence
        if metadata is not None and isinstance(usage, dict):
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                value = usage.get(field)
                if type(value) is int and value >= 0:
                    metadata[field] = value
    except HTTPError as error:
        with error:
            if error.code in (401, 403):
                status = "API_AUTH_OR_ACCESS_ERROR"
            elif error.code == 429:
                status = "API_RATE_OR_QUOTA"
                if metadata is not None:
                    try:
                        payload = json.loads(error.read(8192))
                        problem = payload.get("error") if isinstance(payload, dict) else None
                        metadata["quota_exhausted"] = (
                            isinstance(problem, dict) and problem.get("code") == "insufficient_quota")
                    except (ValueError, OSError):
                        pass
            elif 500 <= error.code < 600:
                status = "API_TRANSIENT_ERROR"
            else:
                status = f"API_HTTP_{error.code}"
            if metadata is not None and error.headers is not None:
                delay = retry_after(error.headers)
                if delay is not None:
                    metadata["retry_after_seconds"] = delay
        return status, None
    except (TimeoutError, URLError) as error:
        reason = error.reason if isinstance(error, URLError) else error
        if isinstance(reason, socket.timeout):
            return "API_TIMEOUT", None
        if isinstance(reason, socket.gaierror):
            return "API_DNS_ERROR", None
        if isinstance(reason, ssl.SSLError):
            return "API_TLS_ERROR", None
        return "API_NETWORK_ERROR", None
    except ValueError:
        return "DECISION_INVALID", None
    return status, choice


def check_connection(*, key=None):
    return request_choice(
        "Synthetic QA state: player is grounded. The objective is to test jumping.",
        {"JUMP": "Test jump while grounded", "WAIT": "Keep the player still"},
        key=key,
    )


def smoke():
    status, choice = check_connection()
    if status == "API_KEY_MISSING":
        print("BLOCKED: OPENAI_API_KEY is missing")
        return 2
    print(f"{status}: {choice}" if choice is not None else status)
    return 0 if status == "OK" else 1


def self_test():
    with open(os.path.join(os.path.dirname(__file__), "decisions_choice.json"), encoding="utf-8") as fixture:
        valid = json.load(fixture)
    answer = valid["answers"][0]
    cases = (
        (valid, ("OK", "JUMP")),
        ({}, ("DECISION_INVALID", None)),
        ({"answers": [answer, answer]}, ("DECISION_INVALID", None)),
        ({"answers": [dict(answer, choice="ATTACK")]}, ("DECISION_INVALID", None)),
        ({"answers": [dict(answer, choice=[])]}, ("DECISION_INVALID", None)),
        ({"answers": [dict(answer, type="refusal")]}, ("DECISION_REFUSED", None)),
    )
    for payload, expected in cases:
        actual = classify_answer(payload, {"JUMP", "WAIT"})
        if actual != expected:
            raise AssertionError((payload, actual, expected))
    print(f"PASS: {len(cases)} offline Choice cases")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--self-test", action="store_true")
    mode.add_argument("--live", action="store_true")
    args = parser.parse_args()
    raise SystemExit(self_test() if args.self_test else smoke())
