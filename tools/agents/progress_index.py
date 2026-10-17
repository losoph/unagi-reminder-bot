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
BRIEF_PROGRESS_WARNING = (
    "⚠ бриф требует дописывать в docs/sprint/progress.md: "
    "с 17.10 решения пишутся в отдельный файл docs/sprint/progress/<МЕТКА>.md"
)


def check_brief_warning(brief: Path | str) -> str | None:
    """Проверяет бриф на требование дописывать в старый docs/sprint/progress.md.

    Возвращает строку предупреждения или None, если бриф корректен.
    """
    if isinstance(brief, Path):
        try:
            content = brief.read_text(encoding="utf-8")
        except OSError:
            return None
    else:
        content = str(brief)

    # Если бриф уже указывает на каталог docs/sprint/progress/ (например docs/sprint/progress/<TASK>.md)
    if "docs/sprint/progress/" in content:
        return None

    # Проверяем строки брифа на указания писать в progress.md
    for line in content.splitlines():
        # Убираем конструкции отрицания: «не дописывай в progress.md», «без правок ... progress.md», «а не в конец progress.md»
        clean_line = re.sub(
            r"(?i)(?:не|без|вместо|а\s+не)\s+[^.\n]*(?:docs/sprint/)?progress\.md",
            "",
            line,
        )
        if re.search(r"(?i)(?:блок|блоком|допис|добав|запис|в|в\s+конец).*(?:docs/sprint/)?progress\.md", clean_line):
            return BRIEF_PROGRESS_WARNING
        if re.search(r"docs/sprint/progress\.md", clean_line):
            return BRIEF_PROGRESS_WARNING

    return None


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
        "> Архив решений до перехода на PRG1 (18.10): [docs/sprint/progress.md](../progress.md)",
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
    parser.add_argument(
        "--check-brief",
        metavar="FILE",
        help="Проверить бриф на требование дописывать в старый progress.md",
    )
    args = parser.parse_args(argv)

    if args.check_brief:
        brief_path = Path(args.check_brief).resolve()
        warn = check_brief_warning(brief_path)
        if warn:
            sys.stderr.write(f"{warn}\n")
        return 0

    dir_path = Path(args.directory).resolve()
    entries = collect_progress_entries(dir_path)
    output_md = format_index_markdown(entries)

    if args.output:
        out_path = Path(args.output).resolve()
        source_paths = {Path(e["path"]).resolve() for e in entries}
        if out_path in source_paths or (
            out_path.parent == dir_path and out_path.name not in IGNORED_FILENAMES and out_path.suffix == ".md"
        ):
            sys.stderr.write(
                f"Ошибка: выходной файл {out_path} совпадает с исходным файлом решения (защита от перезаписи).\n"
            )
            return 1
        out_path.write_text(output_md, encoding="utf-8")
        print(f"Индекс сохранён в {out_path} ({len(entries)} записей)")
    else:
        sys.stdout.write(output_md)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
