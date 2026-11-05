"""Конфигурация: dataclass-схема, tomllib, hot-reload по mtime.

Правишь config.toml — в течение пары секунд все модули видят новые
значения через cfg = config.current. Смена значений публикуется
событием config.changed на шине.
"""
from __future__ import annotations

import logging
import time
import tomllib
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class General:
    character_name: str = "Kuni"
    character_nickname: str = ""          # короткое имя для своих (Ако и т.п.)
    papik_name: str = ""
    papik_chat_id: int = 0
    telegram_api_id: int = 0
    telegram_api_hash: str = ""
    data_dir: str = "data"
    prompts_dir: str = "prompts"
    language: str = "ru"


@dataclass
class TelegramCfg:
    enabled: bool = True
    session_name: str = "kuni"
    tdjson_path: str = ""
    echo_mode: bool = False
    typing_speed_cps: float = 14.0
    reply_delay_min_ms: int = 800
    reply_delay_max_ms: int = 4000
    read_delay_min_ms: int = 1500
    read_delay_max_ms: int = 9000
    night_from: int = 2
    night_to: int = 9
    night_enabled: bool = True           # False = не спит никогда (тесты, отладка)
    premium: bool = False


@dataclass
class EndpointCfg:
    """Переопределение эндпоинта для отдельной функции (вижн, эмбеддинги...).

    Пустое поле = унаследовать из [llm]. temperature=-1 = унаследовать.
    """
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = -1.0


@dataclass
class LLMCfg:
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    model: str = "qwen2.5:14b"
    vision_model: str = "gemini-flash"      # для описания картинок (multimodal)
    fallback_models: list[str] = field(default_factory=list)  # по порядку при rate-limit/ошибке
    temperature: float = 0.8                # креативность ответов (0 = сухо, 1 = шизь)
    embed_model: str = "nomic-embed-text"
    context_soft_cap: int = 20000
    # отдельные эндпоинты: свой API на каждую функцию (опционально)
    vision: EndpointCfg = field(default_factory=EndpointCfg)
    embedding: EndpointCfg = field(default_factory=EndpointCfg)
    emotion: EndpointCfg = field(default_factory=EndpointCfg)
    sleep: EndpointCfg = field(default_factory=EndpointCfg)
    # эмбеддинги не критичны для запуска: без них память деградирует до текста
    embed_required: bool = False


@dataclass
class ProxyCfg:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 10434
    logs_dir: str = "logs_proxy"


@dataclass
class DashboardCfg:
    enabled: bool = True
    # 27.10: host="127.0.0.1" значил «видно только с этой машины».
    # Владелец попросил открыть в локальной сети. "0.0.0.0" слушает все
    # интерфейсы, но тогда дашборд видят все в сети — а у него нет
    # авторизации и есть POST /api/config. Поэтому:
    #   - доступ не с localhost требует токен;
    #   - пустой token + не-localhost = автогенерация в data/dashboard.token.
    host: str = "127.0.0.1"
    port: int = 10435
    token: str = ""        # пусто = только localhost, сеть закрыта


@dataclass
class MetricsCfg:
    enabled: bool = True
    port: int = 9464


@dataclass
class ImagesCfg:
    enabled: bool = True
    url: str = "http://localhost:7860"
    steps: int = 25


@dataclass
class VoiceCfg:
    whisper_url: str = "http://localhost:9000/v1/audio/transcriptions"
    tts_url: str = ""
    tts_voice: str = ""
    tts_model: str = "omniv"


@dataclass
class NewsCfg:
    enabled: bool = True
    channels: list[str] = field(default_factory=list)
    digest_interval_hours: int = 6


@dataclass
class DiaryCfg:
    consolidation_hour: int = 4
    similarity_threshold: float = 0.82
    max_association_entries: int = 5


@dataclass
class BackgroundCfg:
    """Фоновый режим: она в сети всегда, память подшивается по чуть-чуть.

    Вместо одного ночного прогона — короткие проходы по расписанию.
    Ночью (если night_enabled) всё равно уходит в очередь: спать она
    может, но память всё равно подбирает понемногу.
    """
    mode: str = "background"                   # sleep | background
    interval_minutes: float = 20.0             # как часто подметать
    max_episodes_per_pass: int = 8             # потолок работы за проход
    quiet_hours_only: bool = False             # если True — только ночью
    diary_minutes: float = 90.0                # как часто писать дневник
    graph_minutes: float = 45.0                # как часто связывать рёбра
    decay_hour: int = 4                        # раз в сутки: забывание


@dataclass
class WorkingMemoryCfg:
    max_age_days: int = 3


@dataclass
class AffectCfg:
    """Аффективная система: моноамины, гомеостаз, безопасность."""
    enabled: bool = True

    #: Переключатели лечения расщепления «тело против эмоции».
    #: Нужны, чтобы прогнать варианты по отдельности и сравнить.
    variant_a_reconcile: bool = True    # эмоция не может врать про тело
    variant_b_directive: bool = True    # тело как указание, не телеметрия
    variant_c_habituation: bool = True  # насыщение урона и привыкание

    # темперамент (точка притяжения клиренса)
    baseline_s: float = 0.62
    baseline_n: float = 0.38
    baseline_d: float = 0.60

    # сэмплирование: физическое влияние на генерацию
    temperature_base: float = 0.72
    max_tokens_base: int = 700
    steering_enabled: bool = False

    # память: mood-congruent поиск и закрепление подписи
    ecphory_k_base: int = 5
    mood_lambda: float = 0.55
    pad_bind_weight: float = 0.6

    # безопасность
    breaker_enabled: bool = True
    breaker_cooldown_sec: float = 30.0

    # привязанность: вес реплик близкого человека
    w_rel_owner: float = 2.5

    # шум (выключать в тестах)
    noise_enabled: bool = True


@dataclass
class AutoChatCfg:
    """Автозапуск диалога при старте: она пишет сама, без входящего."""
    enabled: bool = False
    chat_id: int = 0                   # кому писать (0 = выключено)
    title: str = ""                    # для логов и промпта
    brief: str = ""                    # её желание, не готовый текст
    delay_seconds: float = 20.0        # подождать, пока поднимутся модули
    repeat_minutes: float = 0.0        # 0 = только при старте


@dataclass
class ProactivityCfg:
    enabled: bool = True                   # общий рубильник её инициатив
    impulse_enabled: bool = True           # A: импульс-петля
    impulse_interval_minutes: int = 45
    thought_stream_enabled: bool = True    # B: поток мыслей + желания
    thought_interval_minutes: int = 15
    needs_enabled: bool = True             # C: «скучаю» по людям
    needs_check_minutes: int = 10
    needs_threshold: float = 70.0          # сколько «скучаю» достаточно, чтобы написать
    miss_rate_per_hour: float = 6.0        # скорость роста «скучаю» (%/час тишины)
    desire_threshold: float = 75.0         # urge желания, при котором она действует
    max_actions_per_day: int = 6
    action_cooldown_minutes: int = 60


@dataclass
class AssistantCfg:
    mode: str = "off"                     # off | companion | assistant (переключается и с дашборда)
    channel_id: int = 0                   # приватный канал-доска заданий (id видно в дашборде)
    check_interval_minutes: int = 5       # как часто читать канал на новые задачи
    run_hour: int = 10                    # во сколько исполнять задачи дня
    agy_command: str = "agy --mode auto --print={task}"
    agy_timeout_minutes: float = 40.0     # лимит на одну задачу антигравити
    report_to_papik: bool = True          # отчитываться в личку создателю


@dataclass
class HarnessCfg:
    """Руки: где ей можно читать и править, и чем кодить."""
    enabled: bool = True
    roots: list[str] = field(default_factory=lambda: [os.getcwd()])
    deny_patterns: list[str] = field(default_factory=lambda: [
        ".env", "config.toml", "id_rsa", "id_ed25519", ".ssh",
        "cookies", "session", "*.pem", "*.key", "*.pfx",
    ])
    max_read_bytes: int = 200_000        # больше — обрезаем с пометкой
    max_write_bytes: int = 400_000
    max_tool_steps: int = 12             # шагов в цикле инструментов за один ответ
    codex_command: str = "codex"
    codex_sandbox: str = "workspace-write"
    vault_path: str = "vault"
    codex_timeout_minutes: float = 25.0
    shell_timeout_seconds: float = 120.0
    require_approval: bool = True        # shell и внешние эффекты — только через кнопку
    audit_log: str = "harness_audit.jsonl"


@dataclass
class MemoryCfg:
    """Нейробиологическая память: лабильный буфер, граф, сон."""
    enabled: bool = True
    labile_ttl_hours: float = 2.0          # жизнь синаптического тега (E-LTP)
    auto_admit: float = 0.62               # балл, при котором захват идёт сразу
    similarity_threshold: float = 0.6      # порог кластеризации при консолидации
    lookback_days: float = 2.0             # окно реплея на сон
    flash_k: int = 4                       # сколько воспоминаний всплывает
    flash_fresh_window_sec: float = 300.0  # не всплывать сразу после записи
    edge_decay: float = 0.02               # синаптическое затухание за ночь
    use_llm_salience: bool = True          # LLM оценивает важность реплики
    use_llm_distill: bool = True           # LLM сжимает кластеры во сне
    max_context_memories: int = 6


@dataclass
class PromptsCfg:
    """Какие слои промптов включать в системный текст."""
    epilogue: bool = True                  # messages_epilogue
    anti_repeat: bool = True               # anti_repeat при повторе
    sticker_vision: bool = True            # sticker_to_text
    image_engineering: bool = True         # image_engineer_*
    image_quality_gate: bool = True        # image_assess_system
    voice_speech: bool = True              # record_audio_speech
    diary_save: bool = True                # diary_save (фоновая запись)


@dataclass
class Config:
    general: General = field(default_factory=General)
    telegram: TelegramCfg = field(default_factory=TelegramCfg)
    llm: LLMCfg = field(default_factory=LLMCfg)
    proxy: ProxyCfg = field(default_factory=ProxyCfg)
    dashboard: DashboardCfg = field(default_factory=DashboardCfg)
    metrics: MetricsCfg = field(default_factory=MetricsCfg)
    images: ImagesCfg = field(default_factory=ImagesCfg)
    voice: VoiceCfg = field(default_factory=VoiceCfg)
    news: NewsCfg = field(default_factory=NewsCfg)
    diary: DiaryCfg = field(default_factory=DiaryCfg)
    background: BackgroundCfg = field(default_factory=BackgroundCfg)
    memory: MemoryCfg = field(default_factory=MemoryCfg)
    affect: AffectCfg = field(default_factory=AffectCfg)
    autochat: AutoChatCfg = field(default_factory=AutoChatCfg)
    prompts: PromptsCfg = field(default_factory=PromptsCfg)
    working_memory: WorkingMemoryCfg = field(default_factory=WorkingMemoryCfg)
    proactivity: ProactivityCfg = field(default_factory=ProactivityCfg)
    assistant: AssistantCfg = field(default_factory=AssistantCfg)
    harness: HarnessCfg = field(default_factory=HarnessCfg)
    source_path: Path | None = None


def _fill(dataclass_obj: Any, data: dict[str, Any]) -> None:
    """Аккуратно заливает dict из toml в dataclass (с вложенными секциями)."""
    for f in fields(dataclass_obj):
        if f.name not in data or data[f.name] is None:
            continue
        current = getattr(dataclass_obj, f.name)
        value = data[f.name]
        if hasattr(current, "base_url") and isinstance(value, dict):
            _fill(current, value)          # вложенная секция (vision/embedding/...)
            continue
        try:
            if isinstance(current, bool):
                value = bool(value)
            elif isinstance(current, int) and not isinstance(current, bool):
                value = int(value)
            elif isinstance(current, float):
                value = float(value)
            elif isinstance(current, str):
                value = str(value)
            elif isinstance(current, list):
                value = [str(x) for x in value] if isinstance(value, list) else current
        except (TypeError, ValueError):
            log.warning("не удалось привести %s=%r, пропускаю", f.name, value)
            continue
        setattr(dataclass_obj, f.name, value)


SECTIONS = ("general", "telegram", "llm", "proxy", "dashboard", "metrics",
            "images", "voice", "news", "diary", "memory", "affect", "autochat",
            "prompts", "working_memory", "proactivity", "assistant", "harness",
            "background")


def load_config(path: Path) -> Config:
    # секреты в config.toml держим ссылками env:NAME, значение — в .env
    from .env import bootstrap_env, resolve_secret
    bootstrap_env(path)
    cfg = Config(source_path=path)
    if not path.exists():
        return cfg
    with open(path, "rb") as fp:
        data = tomllib.load(fp)
    for section_name in SECTIONS:
        section = data.get(section_name)
        if isinstance(section, dict):
            _fill(getattr(cfg, section_name), section)
    # разрешить env:-ссылки на секреты
    llm = cfg.llm
    llm.api_key = resolve_secret(llm.api_key)
    for endpoint in (llm.vision, llm.embedding, llm.emotion, llm.sleep):
        endpoint.api_key = resolve_secret(endpoint.api_key)
    return cfg


def write_default_config(path: Path) -> None:
    example = path.with_name("config.toml.example")
    if example.exists():
        path.write_bytes(example.read_bytes())
    else:
        path.write_text(
            '[general]\ncharacter_name = "Kuni"\ntelegram_api_id = 0\ntelegram_api_hash = ""\n',
            encoding="utf-8",
        )


class ConfigService:
    """Держит текущий Config и перечитывает файл при изменении mtime."""

    CHECK_INTERVAL = 2.0

    def __init__(self, path: Path, bus: Any | None = None) -> None:
        self.path = path
        self.bus = bus
        self._current = load_config(path)
        self._mtime = path.stat().st_mtime if path.exists() else 0.0
        self._checked_at = time.monotonic()

    @property
    def current(self) -> Config:
        return self._current

    def maybe_reload(self, force: bool = False) -> bool:
        """Перечитать конфиг, если файл изменился. True — если перезагрузили."""
        now = time.monotonic()
        if not force and now - self._checked_at < self.CHECK_INTERVAL:
            return False
        self._checked_at = now
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else 0.0
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        self._mtime = mtime
        try:
            fresh = load_config(self.path)
        except Exception:
            log.exception("config.toml битый, продолжаю со старым")
            return False
        old, self._current = self._current, fresh
        changes = diff_configs(old, fresh)
        log.info("config.toml перечитан: %s", changes or "без изменений значений")
        if self.bus is not None and changes:
            self.bus.emit("config.changed", changes=changes)
        return True


def diff_configs(old: Config, new: Config) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for section in fields(old):
        if section.name == "source_path":
            continue
        for f in fields(getattr(old, section.name)):
            ov = getattr(getattr(old, section.name), f.name)
            nv = getattr(getattr(new, section.name), f.name)
            if ov != nv:
                result[f"{section.name}.{f.name}"] = {"was": ov, "now": nv}
    return result


def config_to_dict(cfg: Config) -> dict[str, Any]:
    """Config -> вложенный dict (для веб-редактора)."""
    out: dict[str, Any] = {}
    for section in SECTIONS:
        obj = getattr(cfg, section)
        body: dict[str, Any] = {}
        for f in fields(obj):
            v = getattr(obj, f.name)
            if hasattr(v, "base_url") and is_dataclass(v):    # EndpointCfg
                body[f.name] = {ff.name: getattr(v, ff.name) for ff in fields(v)}
            else:
                body[f.name] = v
        out[section] = body
    return out


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    s = str(v)
    if any(ch in s for ch in '"\\\n'):
        s = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def dict_to_toml_text(data: dict[str, Any]) -> str:
    """dict (как config_to_dict) -> текст config.toml."""
    lines: list[str] = []
    for section, body in data.items():
        if not isinstance(body, dict):
            continue
        lines.append(f"[{section}]")
        for key, value in body.items():
            if isinstance(value, dict):        # вложенная секция [llm.vision]
                lines.append("")
                lines.append(f"[{section}.{key}]")
                for k2, v2 in value.items():
                    lines.append(f"{k2} = {_toml_value(v2)}")
                lines.append("")
            else:
                lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def validate_config_dict(data: dict[str, Any]) -> tuple[Config | None, str]:
    """Прогоняет dict через настоящий загрузчик: (готовый Config, текст toml) или (None, ошибка)."""
    import tempfile
    text = dict_to_toml_text(data)
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False,
                                     encoding="utf-8") as fp:
        fp.write(text)
        path = fp.name
    try:
        cfg = load_config(Path(path))
    except Exception as e:
        return None, str(e)
    finally:
        Path(path).unlink(missing_ok=True)
    return cfg, text
