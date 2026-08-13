from __future__ import annotations

import argparse

from .clients import TodoistClient, WorkflowyClient
from .config import Settings
from .logging import setup_logging
from .service import TransferService
from .storage import Storage


def main() -> None:
    parser = argparse.ArgumentParser(description="Queue Todoist @reference tasks for Workflowy transfer")
    parser.add_argument("command", choices=("collect", "work", "recover"))
    args = parser.parse_args()
    settings = Settings.from_env()
    storage = Storage(settings.database_path)
    todoist = TodoistClient(settings.todoist_token, settings.todoist_rpm, storage)
    workflowy = WorkflowyClient(settings.workflowy_key, settings.workflowy_rpm, storage)
    try:
        service = TransferService(settings, storage, todoist, workflowy, setup_logging())
        getattr(service, args.command)()
    finally:
        todoist.close()
        workflowy.close()
        storage.close()
