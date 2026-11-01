"""Create a Longbridge OAuth token cache in an interactive session.

Headless jobs cannot wait for the browser. After this succeeds, base64 the file
~/.longbridge/openapi/tokens/<client_id> into LONGBRIDGE_OAUTH_TOKEN_CACHE_B64.
"""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Longbridge OAuth SDK token cache")
    parser.add_argument("--client-id", required=True)
    args = parser.parse_args()
    try:
        from longbridge.openapi import OAuthBuilder
    except ImportError:
        print(
            "当前环境没有带 OAuthBuilder 的 Longbridge SDK，无法打开浏览器授权。"
            "安装可用的 longbridge 包后，在可交互环境重试。分析任务会跳过 Longbridge，继续用 YFinance / AkShare。",
            file=sys.stderr,
        )
        return 1
    OAuthBuilder(args.client_id).build()
    print(f"授权完成后，token 缓存在 ~/.longbridge/openapi/tokens/{args.client_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
