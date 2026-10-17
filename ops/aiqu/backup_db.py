#!/usr/bin/env python3
"""Консистентный снимок SQLite перед деплоем: backup API, проверка, атомарная запись, ротация и сжатие.

    python3 ops/aiqu/backup_db.py [--db storage/bot_data.db] [--dir storage/backups] [--keep 14] [--keep-raw 2] [--tag predeploy] [--min-tables 1]

Код выхода 0 — снимок создан и проверен; 1 — снимка нет (деплой нужно остановить); 2 — неверные аргументы.
Ротация трогает только файлы вида bot_data-<время>-<tag>.db[.gz] этого же тега: ручные копии остаются.
Старые снимки сжимаются в gzip, остаётся keep-raw новейших несжатыми для быстрого отката.
"""

import argparse
import gzip
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path


def _tag_regex(db_stem: str, tag: str, suffix: str = r"\.db(?:\.gz)?") -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(db_stem)}-(\d{{8}}-\d{{6}})-{re.escape(tag)}{suffix}$")


def snapshot(db: Path, directory: Path, tag: str, now: float | None = None, min_tables: int = 1) -> tuple[Path, int, int]:
    """Создаёт снимок и возвращает (путь, размер в байтах, число таблиц). Бросает RuntimeError при любой неточности."""
    if not db.is_file():
        raise RuntimeError(f"база не найдена: {db}")
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
    final = directory / f"{db.stem}-{stamp}-{tag}.db"
    partial = final.with_suffix(".db.partial")
    if final.exists():
        raise RuntimeError(f"снимок уже существует: {final}")
    try:
        with closing(sqlite3.connect(str(db))) as source, closing(sqlite3.connect(str(partial))) as target:
            source.backup(target)
            verdict = target.execute("PRAGMA quick_check").fetchone()[0]
            tables = target.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
        if verdict != "ok":
            raise RuntimeError(f"quick_check снимка: {verdict}")
        if tables < min_tables:
            raise RuntimeError(f"число таблиц в снимке: {tables} (ожидалось >= {min_tables})")
        os.chmod(partial, 0o600)
        os.replace(partial, final)
    except (sqlite3.Error, OSError) as exc:
        raise RuntimeError(f"не удалось создать снимок: {exc}") from exc
    finally:
        partial.unlink(missing_ok=True)
    return final, final.stat().st_size, tables


def _verify_gz(gz_path: Path, expected_size: int) -> None:
    """Проверяет целостность gzip, совпадение распакованного размера и quick_check для SQLite."""
    with tempfile.NamedTemporaryFile(suffix=".db.verify", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        try:
            with gzip.open(gz_path, "rb") as gz_in:
                shutil.copyfileobj(gz_in, tmp)
            tmp.flush()
            actual_size = tmp.tell()
            tmp.close()
            if actual_size != expected_size:
                raise RuntimeError(f"размер не совпадает: распаковано {actual_size}, ожидалось {expected_size}")
            if expected_size >= 16:
                with open(tmp_path, "rb") as f:
                    header = f.read(16)
                if header == b"SQLite format 3\x00":
                    with closing(sqlite3.connect(str(tmp_path))) as conn:
                        verdict = conn.execute("PRAGMA quick_check").fetchone()[0]
                        if verdict != "ok":
                            raise RuntimeError(f"quick_check распакованной копии: {verdict}")
        finally:
            tmp_path.unlink(missing_ok=True)


def compress_snapshot(source_db: Path) -> Path:
    """Атомарно сжимает source_db в source_db.gz с проверкой целостности и удалением исходника."""
    final_gz = source_db.with_name(f"{source_db.name}.gz")
    partial_gz = source_db.with_name(f"{source_db.name}.gz.partial")
    partial_gz.unlink(missing_ok=True)
    expected_size = source_db.stat().st_size
    try:
        with open(source_db, "rb") as f_in, gzip.open(partial_gz, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.chmod(partial_gz, 0o600)
        _verify_gz(partial_gz, expected_size)
        os.replace(partial_gz, final_gz)
        source_db.unlink()
        return final_gz
    except Exception as exc:
        partial_gz.unlink(missing_ok=True)
        raise RuntimeError(f"сжатие {source_db.name}: {exc}") from exc


def compress_old_snapshots(db: Path, directory: Path, tag: str, keep_raw: int) -> tuple[int, int]:
    """Сжимает старые несжатые снимки этого тега, оставляя keep_raw новейших несжатыми.

    Возвращает (число успешно сжатых, число оставшихся несжатыми).
    Ошибки сжатия отдельных файлов пишутся в stderr и не приводят к аварийному завершению.
    """
    if not directory.exists():
        return 0, 0
    raw_pattern = _tag_regex(db.stem, tag, suffix=r"\.db")
    raw_files = [p for p in directory.iterdir() if p.is_file() and raw_pattern.match(p.name)]
    raw_files.sort(key=lambda p: (raw_pattern.match(p.name).group(1), p.name))
    to_compress = raw_files[:-keep_raw] if keep_raw > 0 else raw_files
    compressed_count = 0
    for path in to_compress:
        try:
            compress_snapshot(path)
            compressed_count += 1
        except Exception as exc:
            print(f"ПРЕДУПРЕЖДЕНИЕ: {exc}", file=sys.stderr)
    remaining_raw = len([p for p in directory.iterdir() if p.is_file() and raw_pattern.match(p.name)])
    return compressed_count, remaining_raw


def rotate(db: Path, directory: Path, tag: str, keep: int) -> int:
    """Оставляет `keep` новейших снимков этого тега (считая .db и .db.gz); возвращает число удалённых."""
    if not directory.exists():
        return 0
    pattern = _tag_regex(db.stem, tag, suffix=r"\.db(?:\.gz)?")
    snapshots = [p for p in directory.iterdir() if p.is_file() and pattern.match(p.name)]
    snapshots.sort(key=lambda p: (pattern.match(p.name).group(1), p.name))
    removed = snapshots[:-keep] if keep else snapshots
    for path in removed:
        path.unlink()
    return len(removed)


def main(argv: list[str] | None = None, now: float | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="storage/bot_data.db")
    parser.add_argument("--dir", default=None)
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--keep-raw", type=int, default=2)
    parser.add_argument("--tag", default="predeploy")
    parser.add_argument("--min-tables", type=int, default=1)
    args = parser.parse_args(argv)
    if args.keep < 1 or args.keep_raw < 1 or args.min_tables < 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", args.tag):
        print("некорректные --keep, --keep-raw, --min-tables или --tag", file=sys.stderr)
        return 2
    db = Path(args.db)
    directory = Path(args.dir) if args.dir else db.parent / "backups"
    try:
        path, size, tables = snapshot(db, directory, args.tag, now=now, min_tables=args.min_tables)
    except RuntimeError as exc:
        print(f"ОШИБКА резервной копии: {exc}", file=sys.stderr)
        return 1
    # Снимок создан и проверен — дальше только уборка: любой сбой здесь не должен останавливать деплой (код выхода остаётся 0).
    removed = compressed = raw_kept = total_bytes = -1
    try:
        removed = rotate(db, directory, args.tag, args.keep)
        compressed, raw_kept = compress_old_snapshots(db, directory, args.tag, args.keep_raw)
        total_bytes = sum(p.stat().st_size for p in directory.iterdir() if p.is_file())
    except Exception as exc:  # noqa: BLE001
        print(f"ПРЕДУПРЕЖДЕНИЕ: уборка снимков не завершена: {exc}", file=sys.stderr)
    print(
        f"backup ok path={path} size={size} tables={tables} "
        f"rotated_out={removed} compressed={compressed} raw_kept={raw_kept} dir_total={total_bytes}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
