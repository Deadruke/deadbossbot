from main import FREE_TASK_LIMIT, MAX_PARALLEL_TASKS, is_pro_active


def test_default_limits():
    assert FREE_TASK_LIMIT >= 1
    assert MAX_PARALLEL_TASKS >= 1


def test_is_pro_active_false_for_unknown_user():
    assert is_pro_active(-999999) is False
