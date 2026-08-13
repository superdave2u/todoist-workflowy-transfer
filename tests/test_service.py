from __future__ import annotations

from pathlib import Path

from reference_transfer.config import Settings
from reference_transfer.service import TransferService, workflowy_content
from reference_transfer.storage import Storage


class Logger:
    def info(self, _message: str) -> None:
        pass


class FakeTodoist:
    def __init__(self, tasks: list[dict], projects: list[dict]):
        self.tasks, self.project_list = tasks, projects
        self.deleted: list[str] = []

    def active_tasks(self) -> list[dict]:
        return self.tasks

    def reference_tasks(self) -> list[dict]:
        return [item for item in self.tasks if "reference" in item["labels"]]

    def projects(self) -> list[dict]:
        return self.project_list

    def task(self, task_id: str) -> dict:
        return next(item for item in self.tasks if item["id"] == task_id)

    def project(self, project_id: str) -> dict:
        return next(item for item in self.project_list if item["id"] == project_id)

    def children(self, task_id: str) -> list[dict]:
        return [item for item in self.tasks if item.get("parent_id") == task_id]

    def delete_task(self, task_id: str) -> None:
        self.deleted.append(task_id)


class FakeWorkflowy:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.created: list[tuple[str | None, str]] = []
        self.updated: list[tuple[str, str, str]] = []

    def list_root_nodes(self) -> list[dict]:
        return [node for node in self.nodes.values() if node["parent_id"] is None]

    def create_node(self, parent_id: str | None, name: str) -> str:
        node_id = f"node-{len(self.nodes) + 1}"
        self.nodes[node_id] = {"id": node_id, "parent_id": parent_id, "name": name}
        self.created.append((parent_id, name))
        return node_id

    def update_node(self, node_id: str, name: str, note: str) -> None:
        self.nodes[node_id].update(name=name, note=note)
        self.updated.append((node_id, name, note))


def task(task_id: str, content: str, labels: list[str], *, parent_id: str | None = None) -> dict:
    return {
        "id": task_id,
        "content": content,
        "labels": labels,
        "project_id": "project-1",
        "parent_id": parent_id,
        "description": "Details",
        "priority": 2,
    }


def make_service(tmp_path: Path, tasks: list[dict]) -> tuple[TransferService, Storage, FakeTodoist, FakeWorkflowy]:
    storage = Storage(tmp_path / "queue.sqlite3")
    todoist = FakeTodoist(tasks, [{"id": "project-1", "name": "My Project"}])
    workflowy = FakeWorkflowy()
    settings = Settings("todoist", "workflowy", tmp_path / "queue.sqlite3")
    return TransferService(settings, storage, todoist, workflowy, Logger()), storage, todoist, workflowy


def test_workflowy_content_uses_lowercase_project_and_label_tags() -> None:
    name, note = workflowy_content(task("one", "Read this", ["Reference", "Needs Review"]), {"name": "My Project"})
    assert name == "Read this #my-project #needs-review #reference"
    assert "Todoist ID: one" in note
    assert "Project: My Project" in note


def test_leaf_task_is_enqueued_mirrored_and_deleted(tmp_path: Path) -> None:
    service, storage, todoist, workflowy = make_service(tmp_path, [task("one", "Read this", ["reference", "keep"])])
    assert service.collect() == 1
    assert service.work() == 1
    assert todoist.deleted == ["one"]
    mapping = storage.get_mapping("one")
    assert mapping and mapping["status"] == "source_deleted"
    assert workflowy.updated[0][1] == "Read this #keep #my-project #reference"


def test_matching_task_with_untagged_child_is_not_deleted(tmp_path: Path) -> None:
    parent = task("parent", "Parent", ["reference"])
    child = task("child", "Keep source child", [], parent_id="parent")
    service, storage, todoist, _workflowy = make_service(tmp_path, [parent, child])
    service.collect()
    service.work()
    assert todoist.deleted == []
    assert storage.get_mapping("parent")["status"] == "retained"


def test_matching_ancestor_chain_is_created_and_deleted_deepest_first(tmp_path: Path) -> None:
    parent = task("parent", "Parent", ["reference"])
    child = task("child", "Child", ["reference"], parent_id="parent")
    service, _storage, todoist, workflowy = make_service(tmp_path, [parent, child])
    service.collect()
    service.work()
    assert todoist.deleted == ["child", "parent"]
    parent_node = next(node_id for node_id, node in workflowy.nodes.items() if node["name"].startswith("Parent"))
    child_node = next(node for node in workflowy.nodes.values() if node["name"].startswith("Child"))
    assert child_node["parent_id"] == parent_node
