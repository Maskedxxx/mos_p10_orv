"""
Контракты данных сервиса П-10 (шаг 1).

Все модели — pydantic v2, extra="forbid" (лишние поля отвергаются).
КРИТИЧНО: имена полей только латиницей — `claude -p --json-schema` отвергает
кириллические ключи (regex '^[a-zA-Z0-9_.-]{1,64}$'). Значения полей (enum'ы,
строки) могут быть кириллицей.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Допустимые значения вердикта по индикатору
VerdictValue = Literal["нарушен", "соблюдён", "не применимо", "нет данных", "ошибка"]

# Источник показателя данных: открытый (интернет) или запрос органу-разработчику
SourceKind = Literal["открытый", "запрос органу"]


def _not_blank(value: str) -> str:
    """Проверка: строка не пустая и не состоит из одних пробелов после strip."""
    if not value.strip():
        raise ValueError("строка не может быть пустой")
    return value


class Verdict(BaseModel):
    """Вердикт по одному индикатору из 12 (узел [2] пайплайна)."""

    model_config = ConfigDict(extra="forbid")

    indicator: int = Field(ge=1, le=12)
    name: str
    verdict: VerdictValue
    rationale: str
    missing_data: str | None = None
    proof: str | None = None
    clause: str | None = None
    sources: list[str] = Field(default_factory=list)
    attempts: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _check_name_rationale(self) -> "Verdict":
        _not_blank(self.name)
        _not_blank(self.rationale)
        return self

    @model_validator(mode="after")
    def _check_missing_data_consistency(self) -> "Verdict":
        """missing_data осмысленно ТОЛЬКО при verdict == "нет данных" и обязано
        быть заполнено в этом случае; при других вердиктах должно быть None
        (включая verdict == "ошибка" — причина ошибки пишется в rationale)."""
        if self.verdict == "нет данных":
            if self.missing_data is None or not self.missing_data.strip():
                raise ValueError(
                    'при verdict == "нет данных" поле missing_data обязано быть заполнено'
                )
        else:
            if self.missing_data is not None:
                raise ValueError(
                    'поле missing_data допустимо только при verdict == "нет данных"'
                )
        return self


class Buckets(BaseModel):
    """Разбор корзины вердиктов на 4 кучи (узел [3] пайплайна)."""

    model_config = ConfigDict(extra="forbid")

    violated: list[Verdict] = Field(default_factory=list)
    no_data: list[Verdict] = Field(default_factory=list)
    checked: list[Verdict] = Field(default_factory=list)
    failed: list[Verdict] = Field(default_factory=list)


class Effect(BaseModel):
    """Эффект гипотезы: какой показатель и как его считать."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    how_to_calculate: str

    @model_validator(mode="after")
    def _check_not_blank(self) -> "Effect":
        _not_blank(self.metric)
        _not_blank(self.how_to_calculate)
        return self


class DataItem(BaseModel):
    """Один показатель данных, нужный для проверки гипотезы."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    source: SourceKind
    where: str | None = None

    @model_validator(mode="after")
    def _check_not_blank(self) -> "DataItem":
        _not_blank(self.metric)
        return self


class Hypothesis(BaseModel):
    """Одна гипотеза о нарушении индикатора (узлы [4а]/[4б] пайплайна)."""

    model_config = ConfigDict(extra="forbid")

    hypothesis: str
    affected: str
    effect: Effect
    data: list[DataItem] = Field(min_length=1)
    proof: str | None = None
    clause: str | None = None

    @model_validator(mode="after")
    def _check_not_blank(self) -> "Hypothesis":
        _not_blank(self.hypothesis)
        _not_blank(self.affected)
        return self


class HypothesisSet(BaseModel):
    """Набор гипотез по одному индикатору."""

    model_config = ConfigDict(extra="forbid")

    indicator: int = Field(ge=1, le=12)
    name: str
    why_this_many: str
    hypotheses: list[Hypothesis] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_not_blank(self) -> "HypothesisSet":
        _not_blank(self.name)
        _not_blank(self.why_this_many)
        return self


class MapRow(BaseModel):
    """Одна строка итоговой карты гипотез (узел [5] пайплайна)."""

    model_config = ConfigDict(extra="forbid")

    row_id: int
    indicators: list[int] = Field(min_length=1)
    hypothesis: str
    effect: Effect
    data: list[DataItem] = Field(min_length=1)
    proof: str | None = None
    clause: str | None = None

    @model_validator(mode="after")
    def _check_not_blank(self) -> "MapRow":
        _not_blank(self.hypothesis)
        return self


class Merge(BaseModel):
    """Слияние дублирующихся строк карты гипотез."""

    model_config = ConfigDict(extra="forbid")

    merged_row_ids: list[int] = Field(min_length=2)
    reason: str

    @model_validator(mode="after")
    def _check_not_blank(self) -> "Merge":
        _not_blank(self.reason)
        return self


class HypothesisMap(BaseModel):
    """Итоговая карта гипотез с учётом слияний дублей."""

    model_config = ConfigDict(extra="forbid")

    rows: list[MapRow] = Field(default_factory=list)
    merges: list[Merge] = Field(default_factory=list)


class RequestItem(BaseModel):
    """Одна позиция в запросе показателей органу-разработчику."""

    model_config = ConfigDict(extra="forbid")

    num: str
    metric: str
    clause: str | None = None
    for_rows: list[int] = Field(min_length=1)
    breakdown: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_not_blank(self) -> "RequestItem":
        _not_blank(self.num)
        _not_blank(self.metric)
        return self


class DataRequest(BaseModel):
    """Итоговый запрос показателей (узел [6] пайплайна)."""

    model_config = ConfigDict(extra="forbid")

    items: list[RequestItem] = Field(default_factory=list)
    years: list[int] = Field(default_factory=list)


class RunMeta(BaseModel):
    """Метаданные одного прогона пайплайна (для meta.json трейса)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    act_filename: str
    started_at: str
    finished_at: str | None = None
    cost_usd: float = 0.0
    status: str
    current_node: str | None = None

    @model_validator(mode="after")
    def _check_not_blank(self) -> "RunMeta":
        _not_blank(self.run_id)
        _not_blank(self.act_filename)
        _not_blank(self.started_at)
        _not_blank(self.status)
        return self


# ---- Прогресс выполнения прогона (оркестратор app/pipeline.py, файл progress.json) ----

# Состояние одного узла графа выполнения — то, что рисует веб-страница прогона
NodeStatus = Literal["ожидает", "идёт", "готов", "сбой"]

# Состояние прогона целиком
RunStatus = Literal["в очереди", "идёт", "готов", "сбой"]


class NodeState(BaseModel):
    """Состояние одного узла пайплайна в progress.json."""

    model_config = ConfigDict(extra="forbid")

    key: str
    title: str
    status: NodeStatus = "ожидает"
    started_at: str | None = None
    finished_at: str | None = None
    duration_s: float | None = None
    cost_usd: float = 0.0
    error: str | None = None

    @model_validator(mode="after")
    def _check_not_blank(self) -> "NodeState":
        _not_blank(self.key)
        _not_blank(self.title)
        return self


class RunSummary(BaseModel):
    """
    Итоговая сводка прогона — то, что показывает страница по окончании.

    Первые пять чисел — разбор вердиктов узла [2] по значениям (compliant —
    это "соблюдён"; отдельно от Buckets.checked, где "соблюдён" и "не применимо"
    лежат одной кучей).
    """

    model_config = ConfigDict(extra="forbid")

    violated: int = 0
    compliant: int = 0
    not_applicable: int = 0
    no_data: int = 0
    errors: int = 0
    map_rows: int = 0
    merges: int = 0
    request_items: int = 0
    excel_path: str | None = None


class RunProgress(BaseModel):
    """
    Прогресс прогона целиком — отдельный файл progress.json в папке прогона.

    Пишется оркестратором при КАЖДОМ изменении состояния (а не в конце), чтобы
    веб-страница могла читать граф выполнения по ходу. Лежит отдельно от
    meta.json намеренно: meta.json — метаданные прогона, здесь — состояние
    выполнения.

    indicators: "1".."12" -> статус каждого из двенадцати вердиктов узла [2];
    hypotheses_branches: "violated"/"no_data" -> статус веток [4а]/[4б].
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    act_filename: str
    status: RunStatus = "в очереди"
    nodes: list[NodeState] = Field(default_factory=list)
    indicators: dict[str, NodeStatus] = Field(default_factory=dict)
    hypotheses_branches: dict[str, NodeStatus] = Field(default_factory=dict)
    started_at: str
    finished_at: str | None = None
    total_cost_usd: float = 0.0
    summary: RunSummary | None = None

    @model_validator(mode="after")
    def _check_not_blank(self) -> "RunProgress":
        _not_blank(self.run_id)
        _not_blank(self.act_filename)
        _not_blank(self.started_at)
        return self


class RunResult(BaseModel):
    """Результат вызова run_pipeline (app/pipeline.py)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    excel_path: str | None = None
    summary: RunSummary
    total_cost_usd: float = 0.0
    error: str | None = None

    @model_validator(mode="after")
    def _check_not_blank(self) -> "RunResult":
        _not_blank(self.run_id)
        return self
