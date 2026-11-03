"""Training MiniLM on a business's own files, with the Studio's trainer (a separate process, so a crash or the memory it uses cannot take the website down) and its measured results: the same
generated checks, base-versus-tuned metrics and loss curve the Studio shows. The metrics are sanity checks generated from the business's own FAQ, never an accuracy claim (the Studio's report says so)."""
from __future__ import annotations

import json
from pathlib import Path

from ..studio.store import Busy, Store
from .engine import EngineError, LocalEngine


class Trainer:
    def __init__(self, engine: LocalEngine):
        self.engine = engine
        self.store = Store(engine.root / "studio")
        self.map_file = engine.root / "studio_map.json"

    def _map(self) -> dict:
        try:
            return json.loads(self.map_file.read_text())
        except (OSError, ValueError):
            return {}

    def start(self, business_id: str, name: str = "business") -> dict:
        paths = self.engine._files(business_id)
        if not paths:
            raise EngineError(422, "Add a FAQ or a document first: there is nothing to train on yet.")
        m = self._map()
        old = m.get(business_id)
        if old:
            try:
                self.store.delete(old)
            except Exception:  # noqa: BLE001 (a project already gone is fine)
                pass
        try:
            pid = self.store.create_many(name, [(Path(p).name, Path(p).read_bytes()) for p in paths])
            self.store.start(pid, "train")
        except Busy as e:
            raise EngineError(409, str(e)) from None
        except ValueError as e:
            raise EngineError(422, str(e)) from None
        m[business_id] = pid
        self.map_file.parent.mkdir(parents=True, exist_ok=True)
        self.map_file.write_text(json.dumps(m))
        return self.status(business_id)

    def status(self, business_id: str) -> dict:
        pid = self._map().get(business_id)
        if not pid:
            return {"state": "none"}
        st = self.store.status(pid)
        out = {"state": st.get("state", "new"), "stage": st.get("stage"), "error": st.get("error")}
        if out["state"] in ("training", "queued"):
            out["progress"] = self.store.progress(pid)
        rep = self.store.report(pid)
        if rep and out["state"] not in ("training",):
            t, sc = rep.get("training") or {}, rep.get("selfcheck") or {}
            out["report"] = {"entries": (rep.get("faq") or {}).get("entries"), "views": (rep.get("faq") or {}).get("views"), "systems": sc.get("systems"), "checks": sc.get("counts"),
                             "training": {k: t.get(k) for k in ("device", "steps", "epochs", "train_seconds", "train_loss_last", "dev_before", "dev_after")}, "curve": rep.get("curve"),
                             "model": rep.get("model"), "verdict": rep.get("verdict")}
            out["progress"] = self.store.progress(pid)
        return out
