#!/usr/bin/env python3
"""Консистентный снимок SQLite перед деплоем: backup API, проверка, атомарная запись, ротация.

    python3 ops/aiqu/backup_db.py [--db storage/bot_data.db] [--dir storage/backups] [--keep 14] [--tag predeploy]

Код выхода 0 — снимок создан и проверен; 1 — снимка нет (деплой нужно остановить); 2 — неверные аргументы.
Ротация трогает только файлы вида bot_data-<время>-<tag>.db этого же тега: ручные копии остаются.
"""

import argparse
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path


def snapshot(db: Path, directory: Path, tag: str, now: float | None = None) -> tuple[Path, int, int]:
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
        os.chmod(partial, 0o600)
        os.replace(partial, final)
    except (sqlite3.Error, OSError) as exc:
        raise RuntimeError(f"не удалось создать снимок: {exc}") from exc
    finally:
        partial.unlink(missing_ok=True)
    return final, final.stat().st_size, tables


def rotate(db: Path, directory: Path, tag: str, keep: int) -> int:
    """Оставляет `keep` новейших снимков этого тега; возвращает число удалённых."""
    names = sorted(p for p in directory.glob(f"{db.stem}-*-{tag}.db") if p.is_file())
    removed = names[:-keep] if keep else names
    for path in removed:
        path.unlink()
    return len(removed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="storage/bot_data.db")
    parser.add_argument("--dir", default=None)
    parser.add_argument("--keep", type=int, default=14)
    parser.add_argument("--tag", default="predeploy")
    args = parser.parse_args(argv)
    if args.keep < 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", args.tag):
        print("некорректные --keep или --tag", file=sys.stderr)
        return 2
    db = Path(args.db)
    directory = Path(args.dir) if args.dir else db.parent / "backups"
    try:
        path, size, tables = snapshot(db, directory, args.tag)
    except RuntimeError as exc:
        print(f"ОШИБКА резервной копии: {exc}", file=sys.stderr)
        return 1
    removed = rotate(db, directory, args.tag, args.keep)
    print(f"backup ok path={path} size={size} tables={tables} rotated_out={removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
