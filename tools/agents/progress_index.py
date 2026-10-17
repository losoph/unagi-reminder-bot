#!/usr/bin/env python3
"""Сборка индекса журнала решений спринта из docs/sprint/progress/*.md.

Использование:
    python tools/agents/progress_index.py [КАТАЛОГ]
    python tools/agents/progress_index.py --help

Собирает отдельные файлы решений в единый человекочитаемый индекс:
метка, дата коммита, заголовок и ссылка на файл.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
DEFAULT_PROGRESS_DIR = REPO_ROOT / "docs" / "sprint" / "progress"
IGNORED_FILENAMES = {"README.md", "INDEX.md", "index.md"}


def get_commit_date(file_path: Path) -> str:
    """Возвращает дату коммита в формате YYYY-MM-DD или дату mtime, если файл не в git."""
    try:
        res = subprocess.run(
            ["git", "log", "-1", "--format=%cs", "--", file_path.name],
            cwd=str(file_path.parent),
            capture_output=True,
            text=True,
            check=False,
        )
        out = res.stdout.strip()
        if res.returncode == 0 and out:
            return out
    except (OSError, ValueError):
        pass

    try:
        mtime = file_path.stat().st_mtime
        return datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")
    except OSError:
        return "—"


def extract_title(content: str, default: str) -> str:
    """Извлекает первый заголовок Markdown (# или ##) из текста."""
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("#"):
            return re.sub(r"^#+\s*", "", line).strip()
    # Если явного заголовка нет, взять первую непустую строку
    for line in content.splitlines():
        line = line.strip()
        if line:
            return line
    return default


def parse_progress_file(file_path: Path) -> dict[str, Any]:
    """Разбирает файл решения: метка, заголовок, дата коммита, относительный путь."""
    label = file_path.stem
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError:
        content = ""

    title = extract_title(content, default=label)
    commit_date = get_commit_date(file_path)

    return {
        "label": label,
        "title": title,
        "commit_date": commit_date,
        "filename": file_path.name,
        "path": str(file_path),
    }


def collect_progress_entries(directory: Path) -> list[dict[str, Any]]:
    """Находит все *.md файлы в каталоге (кроме служебных индексов) и возвращает записи."""
    if not directory.exists() or not directory.is_dir():
        return []

    entries: list[dict[str, Any]] = []
    for file_path in sorted(directory.glob("*.md")):
        if file_path.name in IGNORED_FILENAMES:
            continue
        entries.append(parse_progress_file(file_path))

    # Сортировка: по дате коммита (по убыванию), затем по метке
    entries.sort(key=lambda item: (item["commit_date"], item["label"]), reverse=True)
    return entries


def format_index_markdown(entries: list[dict[str, Any]], title: str = "Индекс журнала решений") -> str:
    """Форматирует записи в виде таблицы Markdown для удобного чтения человеком."""
    lines: list[str] = [
        f"# {title}",
        "",
        "| Метка | Дата коммита | Заголовок | Файл |",
        "|---|---|---|---|",
    ]

    if not entries:
        lines.append("| — | — | (нет записей) | — |")
        lines.append("")
        return "\n".join(lines)

    for item in entries:
        label = item["label"]
        date = item["commit_date"]
        # Экранируем вертикальные черты в заголовке для таблицы
        entry_title = item["title"].replace("|", "\\|")
        filename = item["filename"]
        lines.append(f"| {label} | {date} | {entry_title} | [{filename}]({filename}) |")

    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сборка индекса журнала решений спринта")
    parser.add_argument(
        "directory",
        nargs="?",
        default=str(DEFAULT_PROGRESS_DIR),
        help=f"Каталог с файлами решений (по умолчанию: {DEFAULT_PROGRESS_DIR})",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Путь к файлу для сохранения индекса (по умолчанию вывод в stdout)",
    )
    args = parser.parse_args(argv)

    dir_path = Path(args.directory).resolve()
    entries = collect_progress_entries(dir_path)
    output_md = format_index_markdown(entries)

    if args.output:
        out_path = Path(args.output).resolve()
        out_path.write_text(output_md, encoding="utf-8")
        print(f"Индекс сохранён в {out_path} ({len(entries)} записей)")
    else:
        sys.stdout.write(output_md)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
