from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    config_version: str = Field(default="1.0.0", description="文件配置结构版本；由插件维护")
    enabled: bool = Field(default=False, description="启用文件读取；还须填写独立QQ授权名单")


class Access(PluginConfigBase):
    group_ids: list[str] = Field(
        default_factory=list, description="允许的QQ群号；空不授权群聊，最多16项"
    )
    user_ids: list[str] = Field(
        default_factory=list, description="允许的私聊QQ号；不是群内成员白名单，最多16项"
    )
    account_id: str = Field(
        default="", description="机器人QQ账号限制；空表示授权目标的已有账号路由"
    )
    group_catalog: bool = Field(
        default=False, description="允许显式请求查询当前群目录；关闭仍可读已登记文件"
    )
    ocr: bool = Field(default=False, description="允许显式PDF单页OCR，会调用现有vlm模型任务")


class Limits(PluginConfigBase):
    file_mib: int = Field(default=20, ge=1, le=40, description="单个文件/压缩包最大MiB")
    cache_mib: int = Field(
        default=256, ge=32, le=1024, description="文件及解析缓存硬额度MiB，包含处理中预留"
    )
    timeout_seconds: int = Field(
        default=100, ge=10, le=180, description="工具整轮期限，包含授权/排队/下载/解析"
    )
    output_chars: int = Field(default=8000, ge=500, le=16000, description="单次正文输出字符上限")
    reads_per_minute: int = Field(
        default=5, ge=1, le=20, description="每聊天每分钟读取/OCR合计上限；不是逻辑轮次预算"
    )


class Config(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    access: Access = Field(default_factory=Access)
    limits: Limits = Field(default_factory=Limits)
