from __future__ import annotations

import hashlib
import html
import json
import random
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from .clients import ApiError, TodoistClient, WorkflowyClient
from .config import Settings
from .logging import event
from .storage import Job, Storage, now

TRIGGER_LABEL = "reference"


def is_eligible(task: dict[str, Any]) -> bool:
    return TRIGGER_LABEL in {str(label).casefold().lstrip("@") for label in task.get("labels", [])}


def tag(value: str) -> str:
    clean = "-".join(value.strip().casefold().lstrip("@#").split())
    return f"#{clean}" if clean else ""


def workflowy_content(task: dict[str, Any], project: dict[str, Any] | None) -> tuple[str, str]:
    tags = {tag(label) for label in task.get("labels", [])}
    if project:
        tags.add(tag(project.get("name", "")))
    title = str(task.get("content", "")).strip()
    name = " ".join(part for part in [title, *sorted(tags)] if part)
    link = f"https://app.todoist.com/app/task/{task['id']}"
    fields = [
        ("Source", f'<a href="{html.escape(link, quote=True)}">Open in Todoist</a>'),
        ("Todoist ID", task["id"]),
        ("Description", task.get("description")),
        ("Project", project.get("name") if project else None),
        ("Section ID", task.get("section_id")),
        ("Labels", ", ".join(f"@{label}" for label in task.get("labels", []))),
        ("Priority", task.get("priority")),
        ("Due", _json_field(task.get("due"))),
        ("Deadline", _json_field(task.get("deadline"))),
        ("Duration", _json_field(task.get("duration"))),
        ("Parent Todoist task", task.get("parent_id")),
    ]
    note = "\n".join(f"{label}: {value}" for label, value in fields if value not in (None, "", {}, []))
    return name, note


def _json_field(value: Any) -> str | None:
    if value in (None, "", {}, []):
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True) if isinstance(value, (dict, list)) else str(value)


def payload_hash(task: dict[str, Any], project: dict[str, Any] | None, ancestors: list[str]) -> str:
    value = {"task": task, "project": project, "eligible_ancestor_ids": ancestors}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def eligible_ancestors(task: dict[str, Any], tasks_by_id: dict[str, dict[str, Any]]) -> list[str]:
    ancestors: list[str] = []
    parent_id = task.get("parent_id")
    while parent_id and parent_id in tasks_by_id:
        parent = tasks_by_id[parent_id]
        if not is_eligible(parent):
            return []
        ancestors.append(parent_id)
        parent_id = parent.get("parent_id")
    return list(reversed(ancestors))


def descendants(task_id: str, tasks: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id = {str(task["id"]): task for task in tasks}
    found: list[dict[str, Any]] = []
    for candidate in tasks:
        parent_id = candidate.get("parent_id")
        while parent_id:
            if parent_id == task_id:
                found.append(candidate)
                break
            parent = by_id.get(parent_id)
            parent_id = parent.get("parent_id") if parent else None
    return found


class TransferService:
    def __init__(self, settings: Settings, storage: Storage, todoist: TodoistClient, workflowy: WorkflowyClient, logger: Any):
        self.settings, self.storage, self.todoist, self.workflowy, self.logger = settings, storage, todoist, workflowy, logger

    def collect(self) -> int:
        tasks = self.todoist.active_tasks()
        projects = {str(project["id"]): project for project in self.todoist.projects()}
        by_id = {str(task["id"]): task for task in tasks}
        count = 0
        for task in tasks:
            if not is_eligible(task):
                continue
            ancestors = eligible_ancestors(task, by_id)
            project = projects.get(str(task.get("project_id")))
            payload = {"task": task, "project": project, "eligible_ancestor_ids": ancestors}
            action = self.storage.enqueue(str(task["id"]), payload, payload_hash(task, project, ancestors))
            if action != "unchanged":
                count += 1
                event(self.logger, "job_enqueued", task_id=task["id"], action=action)
        event(self.logger, "collector_finished", queued=count, active_tasks=len(tasks))
        return count

    def work(self) -> int:
        reclaimed = self.storage.reclaim_expired_leases()
        jobs = self.storage.claim(self.settings.batch_size, self.settings.lease_seconds)
        if not jobs:
            event(self.logger, "worker_finished", claimed=0, reclaimed=reclaimed)
            return 0
        try:
            active_tasks = self.todoist.active_tasks()
            projects = {str(project["id"]): project for project in self.todoist.projects()}
        except ApiError as exc:
            for job in jobs:
                self._retry(job, exc)
            return 0

        by_id = {str(task["id"]): task for task in active_tasks}
        prepared: list[tuple[Job, dict[str, Any], dict[str, Any] | None, list[str]]] = []
        for job in sorted(jobs, key=lambda item: len(item.payload.get("eligible_ancestor_ids", []))):
            task = by_id.get(job.task_id)
            if task is None:
                self._absent_source(job)
                continue
            if not is_eligible(task):
                self.storage.set_job(job.task_id, "cancelled", error="source no longer has reference label")
                if self.storage.get_mapping(job.task_id):
                    self.storage.set_mapping_status(job.task_id, "retained")
                event(self.logger, "job_cancelled", task_id=job.task_id, reason="label_removed")
                continue
            ancestors = eligible_ancestors(task, by_id)
            dependency = ancestors[-1] if ancestors else None
            if dependency and not self.storage.get_mapping(dependency) and self.storage.dependency_open(dependency):
                self.storage.set_job(job.task_id, "pending", next_attempt_at=now() + timedelta(minutes=1), error="waiting for parent")
                event(self.logger, "job_deferred", task_id=job.task_id, dependency=dependency)
                continue
            project = projects.get(str(task.get("project_id")))
            try:
                self._sync_workflowy(job, task, project, dependency)
            except ApiError as exc:
                self._retry(job, exc)
                continue
            prepared.append((job, task, project, ancestors))

        # Delete deepest tasks first. A parent waits until every active descendant is mirrored/deleted.
        for job, task, _project, _ancestors in sorted(prepared, key=lambda value: len(value[3]), reverse=True):
            children = descendants(job.task_id, active_tasks)
            if any(not is_eligible(child) for child in children):
                self.storage.set_job(job.task_id, "retained", error="untagged descendant prevents safe deletion")
                self.storage.set_mapping_status(job.task_id, "retained")
                event(self.logger, "source_retained", task_id=job.task_id, reason="untagged_descendant")
                continue
            if any(not self._descendant_ready(str(child["id"])) for child in children):
                self.storage.set_job(job.task_id, "retry", next_attempt_at=now() + timedelta(minutes=1), error="waiting for descendant transfer")
                event(self.logger, "job_deferred", task_id=job.task_id, reason="descendant_not_ready")
                continue
            try:
                self.todoist.delete_task(job.task_id)
            except ApiError as exc:
                if exc.status_code == 404:
                    self._complete(job)
                else:
                    self._retry(job, exc)
                continue
            self._complete(job)
        event(self.logger, "worker_finished", claimed=len(jobs), reclaimed=reclaimed)
        return len(jobs)

    def _root_id(self) -> str:
        key = "workflowy_root_id"
        saved = self.storage.get_setting(key)
        if saved:
            return saved
        for node in self.workflowy.list_root_nodes():
            if node.get("name") == self.settings.workflowy_root_name:
                self.storage.set_setting(key, node["id"])
                return node["id"]
        root_id = self.workflowy.create_node(None, self.settings.workflowy_root_name)
        self.storage.set_setting(key, root_id)
        event(self.logger, "workflowy_root_created", node_id=root_id)
        return root_id

    def _sync_workflowy(self, job: Job, task: dict[str, Any], project: dict[str, Any] | None, dependency: str | None) -> None:
        name, note = workflowy_content(task, project)
        mapping = self.storage.get_mapping(job.task_id)
        parent_id = self.storage.get_mapping(dependency)["workflowy_node_id"] if dependency and self.storage.get_mapping(dependency) else self._root_id()
        if mapping:
            try:
                self.workflowy.update_node(mapping["workflowy_node_id"], name, note)
                node_id = mapping["workflowy_node_id"]
            except ApiError as exc:
                if exc.status_code != 404:
                    raise
                node_id = self.workflowy.create_node(parent_id, name)
                self.workflowy.update_node(node_id, name, note)
        else:
            node_id = self.workflowy.create_node(parent_id, name)
            self.workflowy.update_node(node_id, name, note)
        self.storage.save_mapping(job.task_id, node_id, job.content_hash, "mirrored")
        event(self.logger, "workflowy_synced", task_id=job.task_id, node_id=node_id)

    def _descendant_ready(self, task_id: str) -> bool:
        mapping = self.storage.get_mapping(task_id)
        # The current worker snapshot still contains this descendant. Only a
        # confirmed delete lets an ancestor delete its whole Todoist subtree.
        return mapping is not None and mapping["status"] == "source_deleted"

    def _absent_source(self, job: Job) -> None:
        if self.storage.get_mapping(job.task_id):
            self._complete(job)
        else:
            self.storage.set_job(job.task_id, "cancelled", error="source task was deleted before transfer")
            event(self.logger, "job_cancelled", task_id=job.task_id, reason="source_missing")

    def _complete(self, job: Job) -> None:
        self.storage.set_job(job.task_id, "completed")
        self.storage.set_mapping_status(job.task_id, "source_deleted")
        event(self.logger, "source_deleted", task_id=job.task_id)

    def _retry(self, job: Job, exc: ApiError) -> None:
        if not exc.retryable:
            self.storage.set_job(job.task_id, "blocked", error=f"{exc.platform} {exc.status_code}: {exc.message}")
            event(self.logger, "job_blocked", task_id=job.task_id, platform=exc.platform, status=exc.status_code)
            return
        exponential = min(3600, 30 * (2 ** job.attempt_count))
        delay = max(exc.retry_after or 0, random.uniform(0, exponential))
        self.storage.set_job(job.task_id, "retry", next_attempt_at=now() + timedelta(seconds=delay),
                             error=f"{exc.platform} {exc.status_code}: {exc.message}", increment_attempt=True)
        event(self.logger, "job_retried", task_id=job.task_id, platform=exc.platform, delay_seconds=round(delay, 2))
