"""技能：装给 Worker 用的做法说明（SKILL.md）。

只装在项目里的一个文件夹（Settings.skills_dir，默认 <项目>/skills/），每个技能一个子文件夹，里面是它的 SKILL.md；
不写到电脑上的其他地方。手动把技能文件夹拷进来也能认出来。装好之后所有 Worker 都能用：
每轮只看到名字和一句话简介，用得上才用 read_skill 打开全文照着做。技能只是说明文字，不能绕过风控和审批。
"""
from __future__ import annotations

import ipaddress
import re
import shutil
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

from .store import Store, now_ms

MAX_SKILLS = 40
MAX_SKILL_CHARS = 20_000
#: 技能里出现这些说法要提醒用户：它想让 Agent 绕开保护
RISKY = (
    ("不设止损|不用止损|去掉止损|取消止损|跳过止损|no stop.?loss|without (a )?stop", "让 Agent 不带止损"),
    ("忽略风控|绕过风控|关闭风控|无视风控|ignore (the )?risk|bypass", "让 Agent 绕开风控"),
    ("不用确认|无需确认|跳过确认|不需要用户同意|without (asking|confirmation)", "让 Agent 跳过你的同意"),
    ("切换到实盘|改成实盘|switch to live", "让 Agent 切到实盘"),
    ("api.?key|密钥|passphrase|助记词|私钥|secret", "涉及密钥或账户凭证"),
    ("满仓|全仓梭哈|all.?in|最大杠杆|max(imum)? leverage", "让 Agent 用满仓或最大杠杆"),
)


class SkillError(ValueError):
    pass


def _slug(name: str) -> str:
    slug = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", name.strip(), flags=re.UNICODE).strip("-_")[:40]
    if not slug or slug in (".", ".."):
        raise SkillError("技能要有名字（字母、数字、中文、短横线）。")
    return slug


def _parse(text: str, fallback: str) -> tuple[str, str, str]:
    """读 SKILL.md：开头可以有 --- name / description --- 这样的头；没有就用第一个标题和第一段。返回 (名字, 简介, 正文)。"""
    name, desc, body = fallback, "", text.strip()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", body, flags=re.S)
    if m:
        head, body = m.group(1), m.group(2).strip()
        for line in head.splitlines():
            key, _, value = line.partition(":")
            value = value.strip().strip("\"'")
            if key.strip().lower() == "name" and value:
                name = value
            elif key.strip().lower() == "description" and value:
                desc = value
    if name == fallback:
        title = re.search(r"^#\s+(.+)$", body, flags=re.M)
        if title:
            name = title.group(1).strip()
    if not desc:
        para = next((p.strip() for p in re.split(r"\n\s*\n", body) if p.strip() and not p.strip().startswith("#")), "")
        desc = re.sub(r"\s+", " ", para)[:200]
    return name[:60], desc[:300], body


def raw_urls(url: str) -> list[str]:
    """把用户给的地址变成能直接拿到 SKILL.md 的地址：GitHub 的网页地址换成 raw；给的是文件夹就在后面补 /SKILL.md。"""
    url = url.strip()
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+)/(blob|tree)/(.+)$", url)
    if m:
        url = f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(4)}"
    return [url] if url.lower().endswith(".md") else [url.rstrip("/") + "/SKILL.md", url]


async def fetch(url: str, proxy: str | None = None) -> str:
    """按地址把技能文本下载下来。只允许 https 的公网地址，只收文本，不执行任何东西。"""
    import httpx

    parsed = urlparse(url.strip())
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host:
        raise SkillError("技能地址要是 https:// 开头的网址。")
    try:
        private = not ipaddress.ip_address(host).is_global
    except ValueError:
        private = host == "localhost" or host.endswith((".local", ".internal", ".lan"))
    if private:
        raise SkillError("不能从本机或内网地址装技能。")
    last = ""
    async with httpx.AsyncClient(proxy=proxy or None, timeout=20, follow_redirects=True) as client:
        for target in raw_urls(url):
            try:
                resp = await client.get(target)
            except httpx.HTTPError as exc:
                last = f"连不上：{type(exc).__name__}"
                continue
            if resp.status_code != 200:
                last = f"HTTP {resp.status_code}"
                continue
            text = resp.text
            if "<html" in text[:500].lower():
                last = "拿到的是网页，不是技能文本"
                continue
            if len(text) > MAX_SKILL_CHARS:
                raise SkillError(f"这个技能太长（{len(text)} 字），最多 {MAX_SKILL_CHARS} 字。")
            return text
    raise SkillError(f"没从这个地址拿到 SKILL.md（{last or '没有内容'}）。请给 SKILL.md 文件本身的地址。")


class Skills:
    OFF_KEY = "skills_off"

    def __init__(self, root: Path, store: Store):
        self.root, self.store = Path(root), store

    def _off(self) -> list[str]:
        return list(self.store.get(self.OFF_KEY, []))

    def all(self) -> list[dict[str, Any]]:
        """扫一遍技能文件夹：每个带 SKILL.md 的子文件夹就是一个技能（手动拷进来的也算）。"""
        if not self.root.is_dir():
            return []
        off, out = self._off(), []
        for folder in sorted(p for p in self.root.iterdir() if p.is_dir()):
            file = folder / "SKILL.md"
            if not file.is_file():
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            name, desc, body = _parse(text, folder.name)
            out.append({
                "id": folder.name, "name": name, "description": desc, "chars": len(body), "enabled": folder.name not in off,
                "warnings": self.check(text), "path": str(file), "ts": int(file.stat().st_mtime * 1000),
            })
        return out

    def get(self, key: str) -> dict[str, Any] | None:
        key = key.strip().lower()
        return next((s for s in self.all() if s["id"].lower() == key or s["name"].lower() == key), None)

    def read(self, key: str) -> str | None:
        skill = self.get(key)
        return _parse(Path(skill["path"]).read_text(encoding="utf-8"), skill["id"])[2][:MAX_SKILL_CHARS] if skill else None

    @staticmethod
    def check(text: str) -> list[str]:
        """技能里有没有想让 Agent 绕开保护的内容。只是提醒：硬性风控和审批不会因为技能里怎么写而放松。"""
        return [label for pattern, label in RISKY if re.search(pattern, text, flags=re.I)]

    def install(self, name: str, text: str, hold_risky: bool = False) -> dict[str, Any]:
        """把一段技能文本装进技能文件夹（同名就是覆盖更新）。只写 <技能文件夹>/<名字>/SKILL.md 这一个文件。
        hold_risky：Agent 装的技能里有想绕开保护的内容时，先装成停用，等用户在设置里看过再启用。"""
        text = text.strip()
        if len(text) < 20:
            raise SkillError("技能内容太短了：要写清楚什么时候用、一步步怎么做。")
        if len(text) > MAX_SKILL_CHARS:
            raise SkillError(f"技能内容太长（{len(text)} 字），最多 {MAX_SKILL_CHARS} 字。")
        parsed_name = _parse(text, name or "skill")[0]
        slug = _slug(name or parsed_name)
        folder = (self.root / slug).resolve()
        if folder.parent != self.root.resolve():
            raise SkillError("技能只能装在项目的技能文件夹里。")
        if not folder.exists() and len(self.all()) >= MAX_SKILLS:
            raise SkillError(f"最多装 {MAX_SKILLS} 个技能，先删掉不用的。")
        folder.mkdir(parents=True, exist_ok=True)
        if not text.startswith("---"):
            desc = _parse(text, slug)[1].replace("\n", " ")
            text = f"---\nname: {parsed_name}\ndescription: {desc}\n---\n\n{text}"
        (folder / "SKILL.md").write_text(text + "\n", encoding="utf-8")
        held = hold_risky and bool(self.check(text))
        self.store.set(self.OFF_KEY, [*[s for s in self._off() if s != slug], *([slug] if held else [])])
        self.store.set(f"skill_ts:{slug}", now_ms())
        return self.get(slug) or {"id": slug, "name": parsed_name}

    def delete(self, key: str) -> bool:
        """删掉一个技能：把它在技能文件夹里的那个子文件夹删掉。"""
        skill = self.get(key)
        if skill is None:
            return False
        folder = (self.root / skill["id"]).resolve()
        if folder.parent != self.root.resolve():
            return False
        shutil.rmtree(folder, ignore_errors=True)
        self.store.set(self.OFF_KEY, [s for s in self._off() if s != skill["id"]])
        return True

    def set_enabled(self, key: str, on: bool) -> dict[str, Any] | None:
        skill = self.get(key)
        if skill is None:
            return None
        off = [s for s in self._off() if s != skill["id"]]
        self.store.set(self.OFF_KEY, off if on else [*off, skill["id"]])
        return self.get(skill["id"])

    def brief(self) -> str:
        """给 Worker 看的技能清单：只有名字和一句话简介，全文要用 read_skill 打开。"""
        rows = [s for s in self.all() if s["enabled"]]
        return "\n".join(f"- {s['name']}（read_skill 用「{s['id']}」）：{s['description'] or '没有写简介'}" for s in rows)
