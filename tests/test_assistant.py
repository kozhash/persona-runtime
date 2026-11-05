"""Тесты ассистент-режима: доска задач, делегирование, режимы."""
import sqlite3
import time

import pytest

from kuni.assistant import AssistantModule
from kuni.core.bus import Bus


class FakeConfigPath:
    def __init__(self, path):
        self.path = path
        self.text = '[assistant]\nmode = "companion"\n'

    def read_text(self, encoding="utf-8"):
        return self.text

    def write_text(self, text, encoding="utf-8"):
        self.text = text


class FakeConfig:
    def __init__(self, path):
        self.current = FakeCfg()
        self.path = FakeConfigPath(path)

    def maybe_reload(self, force=False):
        self.current.assistant.mode = self.text_mode()
        return True

    def text_mode(self):
        import re
        m = re.search(r'mode = "(\w+)"', self.path.text)
        return m.group(1) if m else self.current.assistant.mode


class FakeCfg:
    def __init__(self):
        self.assistant = type("A", (), {
            "mode": "companion", "channel_id": 100, "check_interval_minutes": 5,
            "run_hour": 10, "agy_command": "agy --print {task}",
            "agy_timeout_minutes": 0.01, "report_to_papik": False})()
        self.general = type("G", (), {"papik_chat_id": 0})()


class FakeApp:
    def __init__(self, tmpdir):
        self.config = FakeConfig(tmpdir)
        self.bus = Bus()
        self.db = sqlite3.connect(":memory:")
        self.prompts = type("P", (), {"get": staticmethod(lambda *a, **k: "")})()
        self.telegram = type("T", (), {"chats": {}})()

    async def _chat_resilient_json(self, messages, **kwargs):
        return {"tasks": [
            {"text": "починить упавшие тесты в persona-runtime", "when": "today", "delegate": "agy"},
            {"text": "найти билеты на концерт", "when": "tomorrow", "delegate": "self"},
        ]}


@pytest.mark.asyncio
async def test_parse_creates_tasks(tmp_path):
    app = FakeApp(tmp_path / "config.toml")
    module = AssistantModule(app)
    created = await module._parse_into_tasks(
        "планы: починить упавшие тесты в persona-runtime (сегодня), и на завтра найти билеты на концерт",
        msg_id=55)
    assert len(created) == 2
    assert created[0]["assignee"] == "agy" and created[1]["assignee"] == "self"
    assert created[1]["when"] == "tomorrow"
    # повтор того же сообщения не дублирует задачи
    again = await module._parse_into_tasks("то же", msg_id=55)
    assert module._seen_msg_ids  # msg запомнен
    tasks = module.snapshot()
    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_mode_switch_and_gates(tmp_path):
    app = FakeApp(tmp_path / "config.toml")
    module = AssistantModule(app)
    assert module.mode() == "companion"
    module.set_mode("assistant")
    assert module.mode() == "assistant"
    assert 'mode = "assistant"' in app.config.path.text
    with pytest.raises(ValueError):
        module.set_mode("хрень")
    # в companion задачи не исполняются
    app.config.current.assistant.mode = "companion"
    await module.run_due()
    assert all(t["status"] == "pending" for t in module.snapshot())


@pytest.mark.asyncio
async def test_looks_technical():
    assert AssistantModule._looks_technical("напиши скрипт для парсера")
    assert AssistantModule._looks_technical("пофикси баг в деплое")
    assert not AssistantModule._looks_technical("закажи пиццу на вечер")


def test_mode_requires_assistant_for_run(tmp_path):
    app = FakeApp(tmp_path / "config.toml")
    module = AssistantModule(app)
    # вставим задачу с прошедшим дедлайном напрямую
    module.db.execute(
        "INSERT INTO tasks (msg_id, text, assignee, due_ts, status, created_ts) "
        "VALUES (1,'x','self',?, 'pending', ?)", (time.time() - 10, time.time()))
    module.db.commit()
    import asyncio
    app.config.current.assistant.mode = "off"
    asyncio.run(module.run_due())   # off: ничего не трогает
    assert module.snapshot()[0]["status"] == "pending"


@pytest.mark.asyncio
async def test_react_absorb(tmp_path):
    """#react вырезается из ответа и превращается в реакцию через tdlib."""
    from kuni.telegram.client import IncomingMessage
    app = FakeApp(tmp_path / "config.toml")
    module = AssistantModule(app)
    reactions = []

    async def fake_add_reaction(chat_id, message_id, emoji):
        reactions.append((chat_id, message_id, emoji))
        return True

    app.telegram.add_reaction = fake_add_reaction
    from kuni.app import KuniApp as _KA
    app._REACT_RE = _KA._REACT_RE
    msg = IncomingMessage(chat_id=100, message_id=7, sender_user_id=1, text="лол")
    reply = "лады, эта смешная)) #react 🤣"
    from kuni.app import KuniApp
    clean = await KuniApp._absorb_reactions(app, reply, msg)
    assert reactions == [(100, 7, "🤣")]
    assert "🤣" not in clean and "лады" in clean
    # два эмодзи — обе ставятся
    reactions.clear()
    clean = await KuniApp._absorb_reactions(app, "ну такое #react 👀 #react 🤡", msg)
    assert len(reactions) == 2
    assert "#" not in clean


def test_human_pause_by_mode(tmp_path):
    """Имитация набора — только вне assistant-режима."""
    from kuni.app import KuniApp

    class _Assistant:
        _mode = "assistant"

        def mode(self):
            return self._mode

    class Stub:
        assistant = _Assistant()

    stub = Stub()
    assert KuniApp._human_pause(stub) is False      # ассистент: без театра
    stub.assistant._mode = "companion"
    assert KuniApp._human_pause(stub) is True       # компаньон: имитирует человека
    stub.assistant._mode = "off"
    assert KuniApp._human_pause(stub) is True
