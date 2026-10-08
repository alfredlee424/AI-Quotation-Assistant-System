"""T43/T45 隔離開發唯讀檢查 CLI；不載入應用設定，不授權正式操作。"""
import argparse
import json
import math
from pathlib import Path
import sys


OUTPUT_VERSION = "development-inspection-cli-v1"
WARNING = ("僅隔離開發唯讀檢查；操作聲明不是身份或來源認證。"
           "退出碼 0 只代表所選檢查完成且未發現該範圍差異，不是正式核准。"
           "不證明備份最新、權威來源、停寫或跨庫同一復原點；"
           "不授權報價、確認、保存、修補或恢復配號。")
ARGUMENT_ERROR = "參數無效或未聲明隔離開發用途；請使用 --help，未執行檢查。"
EXECUTION_ERROR = "開發檢查未完成；不提供部分報告，請隔離來源並由開發／DBA 調查。"
COMMANDS = ("schema", "integrity", "registry", "snapshots")


class _ArgumentError(ValueError):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse 預設會回顯未知參數、路徑及輸入值；此邊界刻意不使用 message。
        raise _ArgumentError()


class _Once(argparse.Action):
    def __call__(self, parser, namespace, values, option_string=None):
        seen = getattr(namespace, "_seen", set())
        if self.dest in seen:
            raise _ArgumentError()
        namespace._seen = seen | {self.dest}
        setattr(namespace, self.dest, True if self.nargs == 0 else values)


def _parser():
    parser = _Parser(prog="development_inspection", allow_abbrev=False, description=WARNING)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in COMMANDS:
        sub = commands.add_parser(command, allow_abbrev=False, description=WARNING)
        sub.add_argument("--development-only", action=_Once, nargs=0, required=True,
                         help="明確聲明僅檢查可信隔離開發副本；不是授權憑證")
        if command in ("schema", "integrity"):
            sub.add_argument("--database", action=_Once, required=True, help="既存隔離 SQLite 檔案")
        else:
            sub.add_argument("--reference", action=_Once, required=True, help="參考副本；不認證權威性")
            sub.add_argument("--candidate", action=_Once, required=True, help="候選副本；須為不同檔案")
        sub.add_argument("--timeout", action=_Once, default="2" if command == "schema" else "10",
                         help="大於 0 且至多 30 秒；不是作業系統 I/O 硬中斷保證")
    return parser


def _arguments(argv):
    args = _parser().parse_args(argv)
    try:
        args.timeout = float(args.timeout)
        if not math.isfinite(args.timeout) or not 0 < args.timeout <= 30:
            raise ValueError()
        names = ("database",) if args.command in ("schema", "integrity") else ("reference", "candidate")
        paths = []
        for name in names:
            raw = getattr(args, name)
            if not raw or "://" in raw or raw.lower().startswith("file:") or "\x00" in raw:
                raise ValueError()
            path = Path(raw)
            if path.is_symlink() or not path.is_file():
                raise ValueError()
            # 不將私人設定或工單日誌當成 SQLite 嘗試開啟；不讀取檔案內容。
            resolved = path.resolve(strict=True)
            private_logs = Path(__file__).resolve().parent / "logs"
            if resolved.name.startswith(".env") or resolved.is_relative_to(private_logs):
                raise ValueError()
            setattr(args, name, path)
            paths.append(path)
        if len(paths) == 2 and paths[0].samefile(paths[1]):
            raise ValueError()
    except (ValueError, OSError, RuntimeError):
        raise _ArgumentError() from None
    return args


def _run(args):
    # 僅通過完整參數及操作聲明後才載入指定服務；匯入 CLI 或求助不連資料庫。
    if args.command == "schema":
        from engine.development_schema_inspection import inspect_development_schema
        return inspect_development_schema(args.database, timeout=args.timeout)
    if args.command == "integrity":
        from engine.development_integrity_inspection import inspect_development_integrity
        return inspect_development_integrity(args.database, timeout=args.timeout)
    if args.command == "registry":
        from engine.development_registry_comparison import compare_development_registries
        return compare_development_registries(args.reference, args.candidate, timeout=args.timeout)
    from engine.development_snapshot_comparison import compare_development_snapshots
    return compare_development_snapshots(args.reference, args.candidate, timeout=args.timeout)


def _metadata(report):
    return {"report_version": report.report_version, "warning": report.warning,
            "can_quote": False, "can_confirm": False, "can_save": False,
            "can_resume_numbering": False}


def _integrity_report(report):
    return {**_metadata(report), "table_counts": dict(report.table_counts),
            "verified_saved_batches": report.verified_saved_batches,
            "reservation_only_batches": report.reservation_only_batches,
            "schema_digest": report.schema_digest}


def _document(command, report):
    # 明確投影已知去識別欄位，不能以 asdict / __dict__ 自動匯出未來新增內容。
    result = _metadata(report)
    if command == "schema":
        matched = report.schema_matches
        result.update(schema_matches=matched, expected_digest=report.expected_digest,
                      observed_digest=report.observed_digest, checked_tables=report.checked_tables,
                      issues=[{"table": item.table, "code": item.code} for item in report.issues])
    elif command == "integrity":
        matched = True
        result = _integrity_report(report)
    else:
        matched = report.contents_match
        result.update(contents_match=matched, tables=[{
            "table": item.table, "reference_rows": item.reference_rows,
            "candidate_rows": item.candidate_rows, "missing_rows": item.missing_rows,
            "extra_rows": item.extra_rows, "changed_rows": item.changed_rows,
            "reference_digest": item.reference_digest, "candidate_digest": item.candidate_digest,
        } for item in report.tables])
        if command == "registry":
            result.update(schema_digest=report.schema_digest, identity_conflicts=[
                {"table": item.table, "column": item.column, "count": item.count}
                for item in report.identity_conflicts])
        else:
            result.update(reference_integrity=_integrity_report(report.reference_integrity),
                          candidate_integrity=_integrity_report(report.candidate_integrity),
                          identity_conflicts=[
                              {"table": item.table, "columns": item.columns, "count": item.count}
                              for item in report.identity_conflicts])
    code = 0 if matched else 1
    return {"output_version": OUTPUT_VERSION, "operation": command,
            "status": "completed" if matched else "differences", "exit_code": code,
            "warning": WARNING, "can_quote": False, "can_confirm": False,
            "can_save": False, "can_resume_numbering": False, "report": result}, code


def _error(message, code):
    try:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
    except (OSError, UnicodeError):
        pass
    return code


def main(argv=None):
    try:
        try:
            args = _arguments(argv)
        except _ArgumentError:
            return _error(ARGUMENT_ERROR, 2)
        except SystemExit as result:  # --help 的正常退出；不作資料庫操作。
            return result.code
        document, code = _document(args.command, _run(args))
        # 先完整序列化才寫一次 stdout；ASCII JSON 亦避免終端控制碼及編碼差異。
        encoded = json.dumps(document, ensure_ascii=True, sort_keys=True, allow_nan=False) + "\n"
        sys.stdout.write(encoded)
        sys.stdout.flush()
        return code
    except KeyboardInterrupt:
        return _error("開發檢查已中止；未提供完整報告。", 130)
    except Exception:
        # CLI 最外層遮蔽第三方例外／匯入錯誤／輸出失敗的原文，不印 traceback 或 SQL。
        return _error(EXECUTION_ERROR, 3)


if __name__ == "__main__":
    raise SystemExit(main())
