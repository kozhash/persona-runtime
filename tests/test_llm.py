"""Тесты LLM-слоя: разбор JSON, скиллы, персона."""
from pathlib import Path

import pytest

from kuni.llm.client import extract_json
from kuni.skills import Skill, SkillRegistry
from kuni.skills.builtin import memory_skills


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_in_markdown():
    text = 'Вот ответ:\n```json\n{"emotion": "радость", "valence": 0.8}\n```\nвсё!'
    assert extract_json(text)["emotion"] == "радость"


def test_extract_json_with_prose_around():
    text = 'Подумала и решила: {"action": "merge", "new_text": "x"} — так лучше.'
    assert extract_json(text)["action"] == "merge"


def test_extract_json_garbage_raises():
    with pytest.raises(Exception):
        extract_json("тут совсем нет JSON")


@pytest.mark.asyncio
async def test_memory_skills(tmp_path: Path):
    from kuni.memory.diary import Diary
    from kuni.memory.store import SqliteDiaryStore, DiaryEntry
    from kuni.memory.working import WorkingMemory

    class FakeEmbed:
        async def embed_one(self, text):
            return [1.0, 0.0, 0.0]

    diary = Diary(SqliteDiaryStore(tmp_path / "d.db"), FakeEmbed())
    working = WorkingMemory(tmp_path / "wm.md")
    registry = SkillRegistry()
    for skill in memory_skills(diary, working):
        registry.register(skill)

    assert set(registry.names()) >= {"remember", "recall", "remember_now"}

    result = await registry.dispatch("remember", {"text": "Владу нравится Rust"})
    assert result.ok
    result = await registry.dispatch("remember_now", {"item": "скилить мем"})
    assert result.ok and "мем" in working.content()
    result = await registry.dispatch("recall", {"query": "Влад Rust"})
    assert result.ok and "Rust" in result.output
    result = await registry.dispatch("несуществующий", {})
    assert not result.ok


def test_skill_openai_tool_shape():
    skill = Skill("x", "делает x", {"type": "object", "properties": {}} , lambda a: None)
    tool = skill.to_openai_tool()
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "x"


def test_persona_layers(tmp_path: Path):
    import sqlite3
    from kuni.character.emotions import Emotions
    from kuni.character.persona import PersonaBuilder
    from kuni.character.prompts import PromptLibrary

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "character_base.md").write_text(
        "Ты — {name}, помни: {papik} тебя любит.", encoding="utf-8")
    lib = PromptLibrary(prompts_dir)
    db = sqlite3.connect(":memory:")
    em = Emotions(db)
    em.apply_assessment(
        {"emotion": "нежность", "valence": 0.7, "arousal": 0.2,
         "intensity": 50, "reason": "поздравил"}, 1)

    class CfgStub:
        class current:
            class general:
                character_name = "Куни"
                character_nickname = "Куня"
                papik_name = "Папик"
                language = "ru"

            class assistant:
                mode = "off"

    persona = PersonaBuilder(lib, em, CfgStub)
    system = persona.build(
        chat_title="Влад",
        associations=[{"when": "вчера", "text": " Vlad рассказывал про работу"}],
        working_memory="2026-10-24 - скинуть трек",
    )
    assert "Куни" in system
    # 27.10: воспоминания разделены на две корзины. Ассоциация без
    # speaker_id не может быть «про собеседника» — она уходит в
    # about_others, чтобы модель не приписала её тому, с кем говорит.
    assert "<about_others>" in system and "Vlad" in system
    assert "<things_to_remember>" in system
    assert "нежность" in system and "поздравил" in system


def test_persona_splits_about_them_and_about_others(tmp_path):
    """Воспоминание о текущем собеседнике и о другом человеке — в разных корзинах."""
    import sqlite3
    from kuni.character.emotions import Emotions
    from kuni.character.persona import PersonaBuilder
    from kuni.character.prompts import PromptLibrary

    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "character_base.md").write_text("Ты — {name}.", encoding="utf-8")
    lib = PromptLibrary(prompts_dir)
    db = sqlite3.connect(":memory:")
    em = Emotions(db)

    class CfgStub:
        class current:
            class general:
                character_name = "персонаж"
                character_nickname = "Мэгу"
                papik_name = "собеседник"
                language = "ru"

            class assistant:
                mode = "off"

    persona = PersonaBuilder(lib, em, CfgStub)
    system = persona.build(
        chat_title="собеседник",
        interlocutor_id=111,
        interlocutor_name="собеседник",
        associations=[
            {"when": "вчера", "text": "люблю кофе",
             "speaker_id": 111, "speaker_name": "собеседник"},
            {"when": "3 дня назад", "text": "чай лучше",
             "speaker_id": 222, "speaker_name": "УзбекГПТ",
             "chat_title": "клуб"},
        ],
    )

    assert "Ты сейчас говоришь с собеседник" in system
    assert "<about_them>" in system and "люблю кофе" in system
    assert "<about_others>" in system and "чай лучше" in system

    # чужое не должно попасть в корзину про собеседника
    them = system.split("<about_them>")[1].split("</about_them>")[0]
    assert "чай лучше" not in them
