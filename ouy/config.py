from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """只放部署级配置。欧易的密钥不在这里——授权走欧易官方的 Agent 授权，凭据由欧易官方组件保管。"""

    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", populate_by_name=True)

    ai_model: str = Field("gpt-4o", alias="AI_MODEL")
    ai_base_url: str = Field("https://api.openai.com/v1", alias="AI_BASE_URL")
    ai_api_key: str = Field("", alias="OPENAI_API_KEY")
    ai_api_mode: Literal["auto", "responses", "chat"] = Field("auto", alias="AI_API_MODE")
    # 把固定的系统规则放在请求 input 的最前面（developer 消息），服务端的提示缓存才够得着它；见 agent/core.py 的 lead_instructions
    ai_instructions_first: bool = Field(True, alias="AI_INSTRUCTIONS_FIRST")
    ai_cache_breakpoints: bool = Field(True, alias="AI_CACHE_BREAKPOINTS")  # 在不变资料的末尾标显式缓存断点；上游不认时程序会自己退回自动缓存
    embedding_model: str = Field("BAAI/bge-small-zh-v1.5", alias="KB_EMBEDDING_MODEL")
    proxy_url: str = Field("", alias="LOCAL_PROXY_URL")

    okx_site: Literal["global", "eea", "us", "tr"] = Field("global", alias="OKX_SITE")

    host: str = Field("127.0.0.1", alias="OUY_HOST")
    # 实际监听的地址。只在 Docker 里用：容器内要监听 0.0.0.0，端口由 compose 只发布到宿主机的 127.0.0.1，
    # 所以对外仍然只有本机能访问，OUY_HOST 保持本机地址，Host 校验照常生效。
    bind: str = Field("", alias="OUY_BIND")
    port: int = Field(8899, validation_alias=AliasChoices("OUY_PORT", "PORT"))
    access_key: str = Field("", alias="OUY_ACCESS_KEY")
    data_dir: Path = Field(ROOT / "data", alias="OUY_DATA_DIR")
    # 技能只装在项目里的这一个文件夹（每个技能一个子文件夹，里面是 SKILL.md），不写到电脑上的其他地方
    skills_dir: Path = Field(ROOT / "skills", alias="OUY_SKILLS_DIR")

    @property
    def is_loopback(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1")


@lru_cache
def get_settings() -> Settings:
    return Settings()
