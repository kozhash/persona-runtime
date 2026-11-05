"""Интеграционный прогон: приложение собирается и память работает.

Проверяет то, что не видно в юнит-тестах: KuniApp собирается с новой
памятью, эпизод записывается, всплывает по ассоциации, а сон его
консолидирует. Сеть не нужна: LLM подменяется заглушкой.
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest


class StubLLM:
    """Модель-заглушка: отвечает фиксированным текстом и валидным JSON."""

    def __init__(self):
        self.calls = []
        self.base_url = "stub://"
        self.model = "stub"
        self.embed_model = "stub"

    async def healthy(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None

    async def list_models(self) -> list[str]:
        return [self.model]

    async def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * 32
        for word in (text or "").lower().split():
            vec[hash(word) % 32] += 1.0
        norm = sum(v * v for v in vec) ** 0.5 or 1.0
        return [v / norm for v in vec]

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [await self.embed_one(t) for t in texts]

    async def chat(self, messages, **kwargs):
        from kuni.llm.client import ChatResult
        self.calls.append(messages)
        return ChatResult(content="мхм", finish_reason="stop")

    async def chat_json(self, messages, **kwargs):
        self.calls.append(messages)
        return {"importance": 0.7, "surprise": 0.5, "emotion": 0.6,
                "entities": ["Жан"], "action": "merge", "kind": "EVENT",
                "text": "Жан признался в любви", "tasks": [],
                "summary": "новости", "sd_prompt": "test",
                "caption": "подпись", "positivePrompt": "p", "negativePrompt": "n",
                "satisfied": True, "feedback": "", "emotion": "спокойствие",
                "valence": 0.1, "arousal": 0.3, "reason": "тест", "diary_note": ""}


def make_app(tmp_path: Path):
    """Собрать KuniApp на временной директории, без сети и телеграма."""
    from kuni.app import KuniApp

    config = tmp_path / "config.toml"
    config.write_text(
        '[general]\n'
        'character_name = "Тест"\n'
        'papik_chat_id = 0\n'
        'data_dir = "data"\n'
        'prompts_dir = "prompts"\n'
        '[telegram]\n'
        'enabled = false\n'
        '[llm]\n'
        'base_url = "http://127.0.0.1:9/v1"\n'
        'model = "stub"\n'
        'embed_model = "stub"\n'
        '[proxy]\n'
        'enabled = false\n'
        '[dashboard]\n'
        'enabled = false\n'
        '[metrics]\n'
        'enabled = false\n'
        '[images]\n'
        'enabled = false\n'
        '[assistant]\n'
        'mode = "off"\n'
        '[memory]\n'
        'enabled = true\n'
        'use_llm_salience = false\n'
        'use_llm_distill = false\n',
        encoding="utf-8",
    )
    # промпты берём настоящие из репозитория
    root = Path(__file__).resolve().parents[1]
    app = KuniApp(config)
    app.prompts.directory = root / "prompts"
    app.prompts.reload_all()
    return app


def test_app_builds_with_new_memory(tmp_path: Path):
    app = make_app(tmp_path)
    assert app.diary_store is not None
    assert app.diary is not None
    assert app.sleep_cycle is not None
    # новая схема, а не плоская diary
    tables = {r[0] for r in app.diary_store.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "episodes" in tables
    assert "edges" in tables
    assert "facts" in tables


@pytest.mark.asyncio
async def test_memory_works_inside_app(tmp_path: Path):
    app = make_app(tmp_path)
    app.embed_llm = StubLLM()
    app.diary.embedder = app.embed_llm

    first = await app.diary.add(
        "мы говорили про закопанное тело у гаражей", importance=0.9, promote=True)
    second = await app.diary.add(
        "мы говорили про закопанное тело у гаражей вчера", importance=0.8, promote=True)

    assert app.diary_store.count() == 2
    # похожие по «гаражам» должны связаться
    assert app.diary_store.edge_count() >= 1

    recalled = await app.diary.recall("что там с гаражами", k=2)
    assert recalled, "память не вернула ничего"
    texts = " ".join(r["text"] for r in recalled)
    assert "гараж" in texts

    flash = await app.diary.flash("гаражи", app.emotions.state, k=3,
                                  fresh_window=0)
    assert flash
    assert all("via" in item for item in flash)


@pytest.mark.asyncio
async def test_sleep_consolidates_in_app(tmp_path: Path):
    app = make_app(tmp_path)
    app.embed_llm = StubLLM()
    app.diary.embedder = app.embed_llm

    now = time.time()
    from kuni.memory.episodic import Episode
    for i in range(2):
        text = f"разговор про гаражи номер {i}"
        app.diary_store.add_episode(Episode(
            episode_id=f"t{i}", ts=now - 100, text=text,
            embedding=await app.embed_llm.embed_one(text), importance=0.7))
        app.diary_store.tag(f"t{i}", strength=0.9, ttl_seconds=600, now=now)

    stats = await app.sleep_cycle.run(similarity_threshold=0.4, now=now)
    assert stats.captured == 2
    assert stats.clusters >= 1


def test_prompts_all_resolve(tmp_path: Path):
    """Каждый промпт из prompts/ должен читаться библиотекой."""
    app = make_app(tmp_path)
    root = Path(__file__).resolve().parents[1]
    prompt_files = sorted((root / "prompts").glob("*.md"))
    assert len(prompt_files) >= 23
    for path in prompt_files:
        name = path.stem
        text = app.prompts.get(name, name="X", papik="Y", nickname="Z",
                               CHARACTER_NAME="C", CHARACTER_APPEARANCE_PROMPT="A",
                               SANITIZED_PHOTO_DESCRIPTION="D")
        assert text is not None
        assert text.strip(), f"промпт {name} пуст"


def test_persona_includes_all_layers(tmp_path: Path):
    app = make_app(tmp_path)
    system = app.persona.build(
        chat_title="тест", chat_type="private",
        associations=[{"text": "вспомнилось", "when": "вчера"}],
        working_memory="помнить про гаражи",
        thoughts="надо спросить",
    )
    # слои: личность, поведение, мир чатов, стиль, внешность, память, эпилог
    from pathlib import Path as P
    root = P(__file__).resolve().parents[1]
    base = (root / "prompts" / "character_base.md").read_text(encoding="utf-8")
    first_line = base.strip().splitlines()[0][:30]
    assert first_line in system
    assert "вспомнилось" in system
    assert "помнить про гаражи" in system
    assert "надо спросить" in system
    assert "CURRENT EMOTION" in system
