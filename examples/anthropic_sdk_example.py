#!/usr/bin/env python3
"""最小 Anthropic SDK 调用示例，可直接对接 rita2api。"""
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
    if value.endswith("/v1"):
        return value[:-3].rstrip("/")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用 Anthropic SDK 调用 rita2api")
    parser.add_argument(
        "--base-url",
        default=os.getenv("RITA_ANTHROPIC_BASE_URL") or os.getenv("RITA_BASE_URL") or "http://localhost:10089",
        help="服务地址，可传根地址，也可传带 /v1 的地址，脚本会自动归一化",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("RITA_ANTHROPIC_API_KEY") or os.getenv("RITA_API_KEY") or os.getenv("PROXY_API_KEY"),
        help="rita2api 的 PROXY_API_KEY",
    )
    parser.add_argument(
        "--model",
        default=os.getenv("RITA_ANTHROPIC_MODEL") or os.getenv("RITA_MODEL") or "claude-sonnet-4-5",
        help="要调用的模型名",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=int(os.getenv("RITA_ANTHROPIC_MAX_TOKENS") or "256"),
        help="Anthropic SDK 要求显式传 max_tokens",
    )
    parser.add_argument(
        "--prompt",
        default=os.getenv("RITA_ANTHROPIC_PROMPT") or DEFAULT_PROMPT,
        help="用户输入内容",
    )
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    if not args.api_key:
        parser.error("缺少 --api-key，或请先设置 RITA_ANTHROPIC_API_KEY / RITA_API_KEY / PROXY_API_KEY")

    try:
        from anthropic import Anthropic
    except ImportError:
        print("缺少 anthropic SDK，请先执行：uv run --with anthropic python examples/anthropic_sdk_example.py ...", file=sys.stderr)
        return 1

    client = Anthropic(api_key=args.api_key, base_url=_normalize_base_url(args.base_url))
    response = client.messages.create(
        model=args.model,
        max_tokens=args.max_tokens,
        messages=[{"role": "user", "content": args.prompt}],
    )
    texts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
    print("\n".join(texts) or response.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
