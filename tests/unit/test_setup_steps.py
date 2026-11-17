from knowledge_hub.setup.steps import Change, run


class Fake:
    def __init__(self, name, plan=(), apply=(), error=None):
        self.name = name
        self._plan = list(plan)
        self._apply = list(apply)
        self._error = error
        self.applied = None

    async def plan(self):
        if self._error:
            raise self._error
        return self._plan

    async def apply(self, prune):
        self.applied = prune
        if self._error:
            raise self._error
        return self._apply


def test_render():
    assert Change("+", "group all").render() == "+ group all"
    assert Change("i", "x: y").render() == "i x: y"


async def test_plan_exit_codes(capsys):
    info = Fake("a", plan=[Change("?", "unmanaged label x"), Change("i", "note")])
    assert await run([info], "plan", False) == 0
    for prefix in "+~-!":
        assert await run([Fake("a", plan=[Change(prefix, "thing a")])], "plan", False) == 1
    out = capsys.readouterr().out
    assert "? unmanaged label x\n" in out and "i note\n" in out and "! thing a\n" in out


async def test_apply_exit_codes_and_prune(capsys):
    step = Fake("a", apply=[Change("+", "group a"), Change("?", "unmanaged group b")])
    assert await run([step], "apply", True) == 0
    assert step.applied is True
    assert await run([Fake("a", apply=[Change("!", "group a: changed repo")])], "apply", False) == 2
    assert "+ group a\n? unmanaged group b\n" in capsys.readouterr().out


async def test_error_returns_two_hides_secrets_and_stops(capsys):
    later = Fake("later", plan=[Change("+", "x")])
    err = Fake("postgres", error=RuntimeError("connect failed password=hunter2 host=h"))
    assert await run([err, later], "plan", False) == 2
    out = capsys.readouterr().out
    assert out.startswith("setup: postgres: connect failed")
    assert "hunter2" not in out and "+ x" not in out
    assert await run([err], "apply", False) == 2
