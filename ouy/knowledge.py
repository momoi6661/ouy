"""交易知识库（RAG）：knowledge/ 目录下的 Markdown 切块后建索引，供 Agent 检索。

- 关键词检索：BM25，中文按单字 + 二元组切分，英文/数字按词切分——不依赖任何外部服务。
- 向量检索：本地 embedding 模型（fastembed + bge-small-zh，纯 CPU，不调用任何外部接口），与 BM25 用 RRF 融合。
  模型加载失败时自动退回纯关键词检索，不影响交易。
- knowledge/core/ 下的文档是硬性纪律，除了可被检索，还会固定写进 Agent 的系统提示词。
- 文档改动后自动重建索引，不用重启。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import ROOT, Settings

log = logging.getLogger("ouy.kb")

MAX_CHUNK_CHARS = 450
RRF_K = 60
HF_MIRROR = "https://hf-mirror.com"
_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?|[\u4e00-\u9fff]+")
_HEADING = re.compile(r"^(#{1,3})\s+(.*)$")


@dataclass(frozen=True)
class Chunk:
    id: str
    source: str
    title: str
    section: str
    text: str

    def render(self) -> str:
        head = self.title if self.section == self.title else f"{self.title} › {self.section}"
        return f"【{head}】\n{self.text}"

    def public(self, score: float | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "source": self.source, "title": self.title, "section": self.section, "text": self.text}
        if score is not None:
            out["score"] = round(score, 4)
        return out


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for piece in _WORD.findall(text.lower()):
        if "\u4e00" <= piece[0] <= "\u9fff":
            tokens.extend(piece)
            tokens.extend(piece[i : i + 2] for i in range(len(piece) - 1))
        else:
            tokens.append(piece)
    return tokens


def split_markdown(source: str, text: str) -> list[Chunk]:
    """按 #/##/### 标题切块；过长的小节再按段落切。"""
    title = Path(source).stem
    section = ""
    buf: list[str] = []
    raw: list[tuple[str, str]] = []

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            raw.append((section or title, body))
        buf.clear()

    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            flush()
            if len(m.group(1)) == 1:
                title = m.group(2).strip()
                section = ""
            else:
                section = m.group(2).strip()
        else:
            buf.append(line)
    flush()

    # 小节先按段落切到不超长，再把相邻的短小节合并成一个片段——一条策略的「入场/止损/止盈」尽量待在一起
    pieces: list[tuple[str, str]] = []
    for sec, body in raw:
        current = ""
        for para in re.split(r"\n\s*\n", body):
            if current and len(current) + len(para) > MAX_CHUNK_CHARS:
                pieces.append((sec, current))
                current = para
            else:
                current = f"{current}\n\n{para}" if current else para
        if current:
            pieces.append((sec, current))

    merged: list[tuple[list[str], str]] = []
    for sec, body in pieces:
        block = body if sec == title else f"〔{sec}〕\n{body}"
        if merged and len(merged[-1][1]) + len(block) <= MAX_CHUNK_CHARS:
            names, text_so_far = merged[-1]
            if sec not in names:
                names.append(sec)
            merged[-1] = (names, f"{text_so_far}\n\n{block}")
        else:
            merged.append(([sec], block))

    chunks: list[Chunk] = []
    for i, (names, body) in enumerate(merged):
        digest = hashlib.sha1(f"{source}|{i}|{body}".encode()).hexdigest()[:12]
        chunks.append(Chunk(id=digest, source=source, title=title, section=" / ".join(names), text=body.strip()))
    return chunks


class _BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.k1, self.b = k1, b
        self.tf = [Counter(d) for d in docs]
        self.len = [len(d) for d in docs]
        self.avg = (sum(self.len) / len(docs)) if docs else 0.0
        df: Counter[str] = Counter()
        for d in docs:
            df.update(set(d))
        n = len(docs)
        self.idf = {t: math.log(1 + (n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def scores(self, query: list[str]) -> list[float]:
        out = [0.0] * len(self.tf)
        for term in set(query):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, tf in enumerate(self.tf):
                f = tf.get(term)
                if f:
                    out[i] += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / (self.avg or 1)))
        return out


class KnowledgeBase:
    def __init__(self, settings: Settings, root: Path | None = None):
        self.settings = settings
        self.root = root or ROOT / "knowledge"
        self.cache_path = Path(settings.data_dir) / "kb_vectors.npz"
        self.chunks: list[Chunk] = []
        self._bm25 = _BM25([])
        self._signature: tuple[Any, ...] | None = None
        self._model: Any = None
        self._matrix: Any = None
        self._vector_signature: tuple[Any, ...] | None = None
        self._vector_lock = asyncio.Lock()
        self._embedding_ok = bool(settings.embedding_model)
        self._embedding_error = ""
        self._refresh()

    # ----- 索引 -----
    def _files(self) -> list[Path]:
        return sorted(self.root.rglob("*.md")) if self.root.is_dir() else []

    def _refresh(self) -> None:
        files = self._files()
        signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
        if signature == self._signature:
            return
        chunks: list[Chunk] = []
        for path in files:
            rel = path.relative_to(self.root).as_posix()
            chunks.extend(split_markdown(rel, path.read_text(encoding="utf-8")))
        self.chunks = chunks
        self._bm25 = _BM25([tokenize(f"{c.title} {c.section} {c.section} {c.text}") for c in chunks])
        self._signature = signature
        log.info("知识库已索引：%d 篇文档，%d 个片段", len(files), len(chunks))

    def core_text(self) -> str:
        """knowledge/core/ 下的硬性纪律，固定进系统提示词。"""
        self._refresh()
        core = self.root / "core"
        parts = [p.read_text(encoding="utf-8").strip() for p in sorted(core.glob("*.md"))] if core.is_dir() else []
        return "\n\n".join(parts)

    def catalog(self) -> list[dict[str, str]]:
        """策略目录：knowledge/strategies/ 下每一篇的名字和一句话「什么时候用」。全部给 Agent，让它知道手上有哪些打法。"""
        out = []
        folder = self.root / "strategies"
        for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
            lines = [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
            if not lines:
                continue
            title = lines[0].lstrip("# ").removeprefix("策略：").strip()
            body = [l for l in lines[1:] if not l.startswith("#")]
            out.append({"id": path.stem, "name": title, "when": (body[0] if body else "")[:90]})
        return out

    def catalog_text(self) -> str:
        rows = self.catalog()
        if not rows:
            return ""
        head = (
            "# 策略目录（你手上的全部打法）",
            "下面每一条都有完整的写法（适用行情、入场、止损、止盈、什么时候不做）：选定之后用 read_strategy(名字) 打开全文，再照着落成 plan，并把名字填进 plan.strategy。",
            "这几条要用的价位程序已经算好放在输入里：「前高前低法」（windows）、「变盘后回踩」（【转折】）、「价值区间」（value_area）。"
            "先定局面再选打法，不要每次都用同一条；每篇末尾的「出处与可信度」写明了它有多大把握。",
        )
        return "\n".join([*head, *(f"- {r['name']}：{r['when']}" for r in rows)])

    def read_strategy(self, name: str) -> str | None:
        """按名字（或文件名）打开一篇策略的全文。"""
        key = name.strip().lower()
        for r in self.catalog():
            if key in (r["id"].lower(), r["name"].lower()) or (key and (key in r["name"].lower() or r["name"].split("（")[0].lower() in key)):
                return (self.root / "strategies" / f"{r['id']}.md").read_text(encoding="utf-8")
        return None

    def _safe_doc(self, source: str) -> Path:
        clean = source.strip().replace("\\", "/").lstrip("/")
        if not clean.endswith(".md"):
            clean += ".md"
        root = self.root.resolve()
        target = (root / clean).resolve()
        if root not in target.parents or target.suffix.lower() != ".md":
            raise ValueError("非法的知识库文档路径")
        return target

    def get_doc(self, source: str) -> dict[str, Any]:
        self._refresh()
        path = self._safe_doc(source)
        if not path.is_file():
            raise FileNotFoundError(f"找不到知识库文档：{source}")
        rel = path.relative_to(self.root.resolve()).as_posix()
        content = path.read_text(encoding="utf-8")
        first = next((l.strip().lstrip("# ").strip() for l in content.splitlines() if l.strip()), path.stem)
        category = "core" if rel.startswith("core/") else "strategy" if rel.startswith("strategies/") else "guide"
        return {"source": rel, "title": first, "category": category, "content": content}

    def save_doc(self, source: str, content: str) -> dict[str, Any]:
        if not content.strip():
            raise ValueError("文档内容不能为空")
        path = self._safe_doc(source)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content.rstrip() + "\n", encoding="utf-8")
        self._refresh()
        return self.get_doc(path.relative_to(self.root.resolve()).as_posix())

    def delete_doc(self, source: str) -> None:
        path = self._safe_doc(source)
        rel = path.relative_to(self.root.resolve()).as_posix()
        if rel == "core/discipline.md":
            raise ValueError("核心纪律清单不能删除，可以直接编辑修改")
        if not path.is_file():
            raise FileNotFoundError(f"找不到知识库文档：{source}")
        path.unlink()
        self._refresh()

    def stats(self) -> dict[str, Any]:
        self._refresh()
        docs: dict[str, dict[str, Any]] = {}
        for c in self.chunks:
            cat = "core" if c.source.startswith("core/") else "strategy" if c.source.startswith("strategies/") else "guide"
            d = docs.setdefault(c.source, {"source": c.source, "title": c.title, "category": cat, "chunks": 0, "chars": 0})
            d["chunks"] += 1
            d["chars"] += len(c.text)
        return {
            "documents": sorted(docs.values(), key=lambda x: ({"core": 0, "strategy": 1, "guide": 2}[x["category"]], x["source"])),
            "chunks": len(self.chunks),
            "retrieval": "本地向量 + 关键词混合" if self._embedding_ok else "关键词（BM25）",
            "embedding_model": self.settings.embedding_model if self._embedding_ok else None,
            "embedding_error": self._embedding_error or None,
        }

    # ----- 本地向量模型 -----
    def _load_model(self) -> Any:
        """加载本地 embedding 模型（首次会下载约 90MB 到 data/models）。直连失败时自动换国内镜像重试。"""
        from fastembed import TextEmbedding

        cache = str(Path(self.settings.data_dir) / "models")
        saved = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "HTTP_PROXY", "HF_ENDPOINT")}
        try:
            if self.settings.proxy_url:
                os.environ["HTTPS_PROXY"] = os.environ["HTTP_PROXY"] = self.settings.proxy_url
            try:
                return TextEmbedding(self.settings.embedding_model, cache_dir=cache)
            except Exception as first:  # noqa: BLE001
                log.warning("从 HuggingFace 下载 embedding 模型失败（%s），改用镜像 %s 重试", first, HF_MIRROR)
                os.environ["HF_ENDPOINT"] = HF_MIRROR
                for key in ("HTTPS_PROXY", "HTTP_PROXY"):
                    os.environ.pop(key, None)
                return TextEmbedding(self.settings.embedding_model, cache_dir=cache)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _build_vectors(self) -> None:
        import numpy as np

        if self._model is None:
            self._model = self._load_model()
        ids = [c.id for c in self.chunks]
        cached: dict[str, Any] = {}
        if self.cache_path.is_file():
            try:
                with np.load(self.cache_path, allow_pickle=False) as data:
                    if str(data["model"]) == self.settings.embedding_model:
                        cached = dict(zip(data["ids"].tolist(), data["vectors"]))
            except Exception:  # noqa: BLE001 - 缓存损坏就重算
                cached = {}
        missing = [c for c in self.chunks if c.id not in cached]
        if missing:
            for chunk, vec in zip(missing, self._model.passage_embed([c.render() for c in missing])):
                cached[chunk.id] = vec / (np.linalg.norm(vec) or 1.0)
        self._matrix = np.stack([cached[i] for i in ids]) if ids else None
        self._vector_signature = self._signature
        if missing and ids:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            np.savez(self.cache_path, model=self.settings.embedding_model, ids=np.array(ids), vectors=self._matrix)

    def _vector_rank(self, query: str, top: int) -> list[int]:
        import numpy as np

        if self._vector_signature != self._signature or self._matrix is None:
            self._build_vectors()
        q = next(iter(self._model.query_embed([query])))
        sims = self._matrix @ (q / (np.linalg.norm(q) or 1.0))
        return [int(i) for i in np.argsort(-sims)[:top]]

    async def warmup(self) -> None:
        """启动时在后台线程里准备好向量索引，不阻塞服务。"""
        self._refresh()
        if not self._embedding_ok or not self.chunks:
            return
        try:
            async with self._vector_lock:
                await asyncio.to_thread(self._build_vectors)
            log.info("知识库向量索引就绪（%s）", self.settings.embedding_model)
        except Exception as exc:  # noqa: BLE001
            self._embedding_ok = False
            self._embedding_error = str(exc)
            log.warning("本地向量模型不可用，知识库退回关键词检索：%s", exc)

    # ----- 检索 -----
    async def search(self, query: str, k: int = 5) -> list[tuple[Chunk, float]]:
        self._refresh()
        if not self.chunks or not query.strip():
            return []
        bm = self._bm25.scores(tokenize(query))
        ranked_bm = [i for i in sorted(range(len(bm)), key=lambda i: -bm[i]) if bm[i] > 0]
        if self._embedding_ok:
            pool = max(k * 4, 20)
            try:
                async with self._vector_lock:
                    ranked_vec = await asyncio.to_thread(self._vector_rank, query, pool)
                fused: dict[int, float] = {}
                for ranking in (ranked_bm[:pool], ranked_vec):
                    for rank, idx in enumerate(ranking):
                        fused[idx] = fused.get(idx, 0.0) + 1 / (RRF_K + rank)
                top = sorted(fused, key=lambda i: -fused[i])[:k]
                return [(self.chunks[i], fused[i]) for i in top]
            except Exception as exc:  # noqa: BLE001 - 向量不可用就退回关键词检索
                self._embedding_ok = False
                self._embedding_error = str(exc)
                log.warning("本地向量模型不可用，知识库退回关键词检索：%s", exc)
        return [(self.chunks[i], bm[i]) for i in ranked_bm[:k]]

    async def context(self, queries: list[str], k: int = 6, max_chars: int = 3000) -> list[Chunk]:
        """多条查询合并去重，按轮转取前 k 个片段，控制总长度。"""
        results = [await self.search(q, k) for q in queries if q.strip()]
        picked: list[Chunk] = []
        seen: set[str] = set()
        size = 0
        for rank in range(k):
            for hits in results:
                if rank < len(hits):
                    chunk = hits[rank][0]
                    # core/ 下的纪律每一轮都固定在系统提示词里，不再占检索的名额
                    if chunk.id in seen or chunk.source.startswith("core/") or size + len(chunk.text) > max_chars:
                        continue
                    seen.add(chunk.id)
                    picked.append(chunk)
                    size += len(chunk.text)
                    if len(picked) >= k:
                        return picked
        return picked
