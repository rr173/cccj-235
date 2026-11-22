"""agrimosaic 命令行：离线入仓 build 与矩形窗口提取 extract。"""

from __future__ import annotations

import argparse
import json
import sys

from .extract import extract_window, validate_window
from .model import InputError
from .store import build_package, load_package


def _cmd_build(args: argparse.Namespace) -> int:
    try:
        summary = build_package(args.config, args.out)
    except InputError as exc:
        print(f"构建被拒收：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def _cmd_extract(args: argparse.Namespace) -> int:
    try:
        pkg = load_package(args.package)
        window = (args.x0, args.y0, args.x1, args.y1)
        validate_window(window, args.target_size)
        result = extract_window(pkg, window, args.target_size)
    except (InputError, ValueError, FileNotFoundError) as exc:
        print(f"提取失败：{exc}", file=sys.stderr)
        return 2
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agrimosaic",
        description="共原点整数格网航扫反射/质量值的离线入仓与窗口提取（不投影、不插值、不栅格化）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_build = sub.add_parser("build", help="按构建配置离线构建标准格网镶嵌表")
    p_build.add_argument("--config", required=True, help="构建配置 JSON 路径")
    p_build.add_argument("--out", required=True, help="输出镶嵌表目录（不可已存在）")
    p_build.set_defaults(func=_cmd_build)

    p_ex = sub.add_parser("extract", help="按矩形窗口与目标分辨率提取目标格")
    p_ex.add_argument("--package", required=True, help="build 产出的镶嵌表目录")
    p_ex.add_argument("--x0", type=int, required=True)
    p_ex.add_argument("--y0", type=int, required=True)
    p_ex.add_argument("--x1", type=int, required=True)
    p_ex.add_argument("--y1", type=int, required=True)
    p_ex.add_argument("--target-size", type=int, required=True,
                      help="目标格边长（基本单元的 2 的非负整数次幂）")
    p_ex.add_argument("--output", help="可选：结果 JSON 输出文件，缺省打印到标准输出")
    p_ex.set_defaults(func=_cmd_extract)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
