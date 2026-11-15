from minihci.tasks.executor import TaskType

REGISTRY: dict[str, TaskType] = {}


def register(tt: TaskType) -> TaskType:
    REGISTRY[tt.name] = tt
    return tt
