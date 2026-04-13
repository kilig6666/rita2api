#!/usr/bin/env python3
"""最小 OpenAI SDK 调用示例，可直接对接 rita2api。"""
from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import urlparse


DEFAULT_PROMPT = "你好，请用一句话介绍你自己。"


def _normalize_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("base_url 必须是完整 URL，例如 http://localhost:10089")
    if parsed.path.endswith("/v1"):
        return value
    return f"{value}/v1"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用 OpenAI SDK 调用 rita2api")
    parser.add_argument(
        "--base-url",
        default=os.getenv("RITA_OPENAI_BASE_URL") or os.getenv("RITA_BASE_URL") or "http://localhost:10089",
        help="服务地址，可传根地址，脚本会自动补 /v1",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("RITA_OPENAI_API_KEY") or os.getenv("RITA_API_KEY") or os.getenv("PROXY_API_KEY"),
        help="rita2api 的 PROXY_API_KEY",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("RITA_OPENAI_MODEL") or os.getenv("RITA_MODEL") or "model_25",
        help="要调用的模型名",
    )
    parser.add_argument(
        "--api",
        choices=["chat", "responses"],
        default=os.getenv("RITA_OPENAI_API") or "chat",
        help="使用 chat.completions 还是 responses",
    )
    parser.add_argument(
        "--prompt",
        default=os.getenv("RITA_OPENAI_PROMPT") or DEFAULT_PROMPT,
        help="用户输入内容",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.api_key:
        parser.error("缺少 --api-key，或请先设置 RITA_OPENAI_API_KEY / RITA_API_KEY / PROXY_API_KEY")

    try:
        from openai import OpenAI
    except ImportError:
        print("缺少 openai SDK，请先执行：uv run --with openai python examples/openai_sdk_example.py ...", file=sys.stderr)
        return 1

    client = OpenAI(api_key=args.api_key, base_url=_normalize_base_url(args.base_url))

    if args.api == "responses":
        response = client.responses.create(model=args.model, input=args.prompt)
        text = getattr(response, "output_text", "")
        print(text or response.model_dump_json(indent=2))
        return 0

    response = client.chat.completions.create(
        model=args.model,
        messages=[{"role": "user", "content": args.prompt}],
    )
    message = response.choices[0].message
    print(message.content or response.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
