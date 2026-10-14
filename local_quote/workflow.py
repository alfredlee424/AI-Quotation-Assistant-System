"""UI 工作階段：變動撤銷固定版本；讀取／選候選不計價或保存。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .business import analyze, generate, input_digest


@dataclass
class Workspace:
    category: str | None = None
    source: str = ""
    choices: dict = field(default_factory=dict)
    catalog: dict | None = None
    analysis: dict | None = None
    result: dict | None = None
    saved: dict | None = None
    generation: int = 0

    def set_input(self, category, source):
        if (category, source) != (self.category, self.source):
            self.category, self.source = category, source
            self.choices = {}
            self.catalog = self.analysis = self.result = self.saved = None
            self.generation += 1

    def choose(self, key, value):
        if self.analysis is None:
            raise ValueError("尚未產生候選")
        question = next((q for q in self.analysis["questions"] if q["key"] == key), None)
        if question is None or (value is not None and value not in [c["value"] for c in question["candidates"]]):
            raise ValueError("選擇不在目前合法候選")
        if value is None:
            self.choices.pop(key, None)
        else:
            self.choices[key] = value
        self.result = self.saved = None
        self.analysis = analyze(self.source, self.catalog, self.choices)
        # 上游材料變更使舊下游候選失效：撤回舊選擇而不是偷偷沿用。
        for _ in range(len(self.choices) + 1):
            valid = {q["key"]: {c["value"] for c in q["candidates"]}
                     for q in self.analysis["questions"]}
            stale = [k for k, v in self.choices.items() if v not in valid.get(k, set())]
            if not stale:
                break
            for k in stale:
                self.choices.pop(k)
            self.analysis = analyze(self.source, self.catalog, self.choices)

    def generate(self, master):
        self.result = self.saved = None
        if not self.category or not self.source.strip():
            raise ValueError("請先選產品類別並貼上四品項需求")
        self.catalog = master.catalog(self.category)
        self.result = generate(self.source, self.catalog, self.choices)
        self.analysis = self.result["analysis"]
        return self.result

    def confirm(self, store):
        if not self.result or not self.result.get("frozen") or self.catalog is None:
            raise ValueError("完整固定成本尚未產生或已失效")
        if self.catalog["prodkind"] != self.category:
            raise ValueError("產品類別已變更，請重新產生")
        digest = input_digest(self.source, self.catalog, self.choices)
        if digest != self.result["analysis"]["input_digest"]:
            raise ValueError("內容已變更，請重新產生")
        self.saved = store.save(self.result["frozen"], confirmed=True, input_digest=digest)
        return self.saved
