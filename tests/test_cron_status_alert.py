"""Версионируемый cron для разового письма с итогами спринта: формат, дата, файл тела и установка деплоем."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRON = ROOT / "ops" / "aiqu" / "cron.d" / "unagi-status-alert"


class StatusAlertCronTests(unittest.TestCase):
    def job_line(self):
        lines = [line for line in CRON.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#") and "=" not in line.split()[0]]
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_job_runs_once_on_sunday_at_19_moscow_and_reads_an_existing_body_file(self):
        fields = self.job_line().split(None, 6)
        self.assertEqual(fields[:5], ["0", "19", "18", "10", "*"])
        self.assertEqual(fields[5], "root")
        command = fields[6]
        self.assertIn("--once 2026-10-18", command)
        body = re.search(r"--file (\S+)", command).group(1)
        self.assertTrue((ROOT / body).is_file(), body)

    def test_deploy_installs_cron_files_from_the_repo(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        self.assertIn("ops/aiqu/cron.d/*", workflow)
        self.assertIn("/etc/cron.d/", workflow)

    def test_body_file_is_not_empty_and_mentions_results(self):
        text = (ROOT / "docs" / "ALERT-SPRINT-2026-10-18.md").read_text(encoding="utf-8")
        self.assertGreater(len(text), 1500)
        self.assertIn("ИТОГИ СПРИНТА", text)


if __name__ == "__main__":
    unittest.main()
