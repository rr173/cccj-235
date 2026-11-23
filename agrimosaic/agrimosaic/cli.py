"""agrimosaic 命令行：离线入仓 build、窗口提取 extract、季末台账 season 与批次质量审计 audit。"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from .audit import AUDIT_FAIL, parse_audit_config, run_audit
from .extract import extract_window, validate_window
from .model import InputError
from .season import run_season_job
from .store import build_package, load_package

# 退出码：0 成功；1 审计有批次未通过（报告已写出）；2 输入错误（无报告）
EXIT_OK = 0
EXIT_AUDIT_FAILED = 1
EXIT_INPUT_ERROR = 2


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


def _cmd_season(args: argparse.Namespace) -> int:
    try:
        ledger = run_season_job(args.manifest)
    except (InputError, ValueError, FileNotFoundError) as exc:
        print(f"季末作业失败：{exc}", file=sys.stderr)
        return 2
    text = json.dumps(ledger, ensure_ascii=False, indent=2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    """批次质量审计：先完整校验并计算，成功后才写出报告。

    stdout 模式不会先打印部分 JSON；文件模式在输入错误时保持既有目标文件原样。
    """
    try:
        config = parse_audit_config(args.config)
        report = run_audit(config)
    except (InputError, ValueError, FileNotFoundError, OSError) as exc:
        print(f"批次审计被拒收：{exc}", file=sys.stderr)
        return EXIT_INPUT_ERROR
    # 先完成全部序列化，再一次性写出：stdout 模式不会先打印半截 JSON。
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        # 先写临时文件再原子替换：写出中途失败也不会留下半截报告。
        directory = os.path.dirname(os.path.abspath(args.output)) or "."
        try:
            fd, tmp = tempfile.mkstemp(prefix=".agrimosaic-audit-", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text + "\n")
                os.replace(tmp, args.output)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
        except OSError as exc:
            print(f"批次审计报告写出失败：{exc}", file=sys.stderr)
            return EXIT_INPUT_ERROR
    else:
        print(text)
    return EXIT_AUDIT_FAILED if report["summary"]["status"] == AUDIT_FAIL else EXIT_OK


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

    p_season = sub.add_parser(
        "season",
        help="季末离线作业：按轮次清单产出逐地块复测资格台账（只读输入包，不做逐位置输出）")
    p_season.add_argument("--manifest", required=True,
                          help="季末清单 JSON：地块、按日严格递增的轮次与包路径、裁决门槛")
    p_season.add_argument("--output", help="可选：台账 JSON 输出文件，缺省打印到标准输出")
    p_season.set_defaults(func=_cmd_season)

    p_audit = sub.add_parser(
        "audit",
        help="批次质量审计：按规则核对包中来源批次的原始数值异常（只读，不改包）")
    p_audit.add_argument("--config", required=True, help="审计配置 JSON 路径")
    p_audit.add_argument("--output", help="可选：报告 JSON 输出文件，缺省打印到标准输出")
    p_audit.set_defaults(func=_cmd_audit)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
