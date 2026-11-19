"""SDK declarations and serialized lifecycle; workflows live in FileService."""

import asyncio

from maibot_sdk import HookHandler, MaiBotPlugin, Tool
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder, ToolParameterInfo, ToolParamType

from .config import Config
from .service import FileService


CONTRACT = "neko.tools.context.v1"


def param(name, description, kind=ToolParamType.STRING, required=False):
    return ToolParameterInfo(name=name, description=description, param_type=kind, required=required)


class NekoFiles(MaiBotPlugin):
    config_model = Config

    def __init__(self):
        super().__init__()
        self.service = FileService()
        self.lifecycle = asyncio.Lock()

    @property
    def ready(self):
        return self.service.ready

    async def on_load(self):
        async with self.lifecycle:
            await self.service.start(self.ctx, self.config)

    async def on_unload(self):
        async with self.lifecycle:
            await self.service.stop()

    async def on_config_update(self, scope, config_data, version):
        async with self.lifecycle:
            await self.service.stop()
            await self.service.start(self.ctx, self.config)

    @HookHandler(
        "chat.receive.after_process",
        name="neko_files_index",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=4000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def receive(self, message=None, **kwargs):
        return await self.service.receive(message, kwargs)

    @Tool(
        "neko_files_list",
        description="在近期索引中查找聊天附件或群文件，获取文件编号。",
        parameters=[
            param("include_group", "默认仅查近7天登记索引，空结果不代表没有附件；明确请求且管理员已开启时设为true查询当前群目录", ToolParamType.BOOLEAN),
            param("folder_id", "子目录真实编号，只用返回值；群根目录省略，不猜0/root"),
            param("cursor", "上次的next_cursor，续读5分钟作用域快照；只填此项。若schema要求session_id，沿用上次真实目标会话的session_id"),
            param("message_id", "真实消息ID或引用所在消息ID；最多一跳，与目录/游标互斥；不猜短编号"),
        ],
        platforms=["qq"],
        trusted_context=CONTRACT,
        timeout_ms=190000,
    )
    async def list_files(self, include_group=False, folder_id="", cursor="", message_id="", **kwargs):
        return await self.service.list_files(
            kwargs.get("_host_context"), include_group, folder_id, cursor, message_id
        )

    @Tool(
        "neko_files_inspect",
        description="按文件编号查看文档结构、PDF 预览或压缩包目录。",
        parameters=[param("file_ref", "list返回的当前聊天ref", required=True),
                    param("offset", "压缩包目录分页用上次next_offset；正文不用此参数", ToolParamType.INTEGER)],
        platforms=["qq"],
        trusted_context=CONTRACT,
        timeout_ms=190000,
    )
    async def inspect_file(self, file_ref="", offset=0, **kwargs):
        return await self.service.inspect(kwargs.get("_host_context"), file_ref, offset)

    @Tool(
        "neko_files_read",
        description="按文件编号读取、检索或续读文档及 PDF 正文。",
        parameters=[
            param("file_ref", "当前聊天中list返回的ref；续读压缩包成员用archive_read返回的member_file_ref", required=True),
            param("start", "开始页/行/段落，从1开始", ToolParamType.INTEGER),
            param("end", "结束位置；指定范围读完即停止，0为自动100单位续页，关键词时0为已解析末尾；OCR必须start=end", ToolParamType.INTEGER),
            param("sheet", "XLSX工作表名"),
            param("keyword", "在start/end范围和所选工作表内不区分大小写查找；空为普通读取"),
            param("offset", "结果文本的字符偏移，不是搜索起点。先按next_offset读完本批，保持其它参数", ToolParamType.INTEGER),
            param("ocr", "显式PDF单页OCR：start=end、offset>=0，不带sheet/keyword/match_offset；需管理员开启，会调用配置模型。续读保ocr=true，只读缓存；unknown或成功缓存丢失均不重做", ToolParamType.BOOLEAN),
            param("match_offset", "命中批次偏移；本批next_offset为空后用next_match_offset，并将offset清零。优先照返回next_call", ToolParamType.INTEGER),
        ],
        platforms=["qq"],
        trusted_context=CONTRACT,
        timeout_ms=190000,
    )
    async def read_file(
        self,
        file_ref="",
        start=1,
        end=0,
        sheet="",
        keyword="",
        offset=0,
        ocr=False,
        match_offset=0,
        **kwargs,
    ):
        return await self.service.read(
            kwargs.get("_host_context"),
            file_ref,
            start,
            end,
            sheet,
            keyword,
            offset,
            ocr,
            match_offset,
        )

    @Tool(
        "neko_files_archive_read",
        description="按压缩包编号和成员名读取其中一份文档。",
        parameters=[
            param("file_ref", "原压缩包编号", required=True),
            param("member", "先用inspect分页查目录，再填写完整成员name", required=True),
        ],
        platforms=["qq"],
        trusted_context=CONTRACT,
        timeout_ms=190000,
    )
    async def read_archive(self, file_ref="", member="", **kwargs):
        return await self.service.read_archive(kwargs.get("_host_context"), file_ref, member)


def create_plugin():
    return NekoFiles()
