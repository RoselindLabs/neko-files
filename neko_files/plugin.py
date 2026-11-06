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
        brief_description="找聊天中的文件先调用本工具取得ref，再用inspect/read的file_ref读取。可查群目录；根目录省略folder_id。失败/未读完不等于没有文件。",
        detailed_description="只返回索引，不下载。include_group仅用于明确查询且管理员开启的当前群目录。next_cursor续读5分钟作用域快照；来源超限会标记source_truncated。",
        parameters=[
            param("include_group", "查询当前群目录", ToolParamType.BOOLEAN),
            param("folder_id", "子目录真实编号，只用返回值；群根目录省略，不猜0/root"),
            param("cursor", "上次的next_cursor，续读只填此项"),
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
        brief_description="查看一个文件的结构、有限预览或压缩包目录；不是全文。",
        detailed_description="只读当前聊天file_ref。预览不是全文；扫描PDF须管理员授权后显式单页OCR。",
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
        brief_description="按页/行/段落范围读取文档正文、关键词查找、续读，或显式单页OCR。",
        detailed_description="页/行/段从1开始。关键词先按next_offset读完当前批，再用next_match_offset查下批。OCR仅显式单页，续读只读缓存，未知不重试。",
        parameters=[
            param("file_ref", "当前聊天文件编号", required=True),
            param("start", "开始页/行/段落，从1开始", ToolParamType.INTEGER),
            param("end", "结束位置；0为默认100单位，关键词时0为已解析末尾；OCR必须start=end", ToolParamType.INTEGER),
            param("sheet", "XLSX工作表名"),
            param("keyword", "在start/end范围和所选工作表内不区分大小写查找；空为普通读取"),
            param("offset", "结果文本的字符偏移，不是搜索起点。先按next_offset读完本批，保持其它参数", ToolParamType.INTEGER),
            param("ocr", "显式PDF单页OCR，必须start=end；需管理员开启。续读保ocr=true；unknown不重试", ToolParamType.BOOLEAN),
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
        brief_description="读取压缩包中一份文档；先inspect分页找完整成员name，不递归不执行。返回member_file_ref可作read的file_ref续读。",
        detailed_description="先inspect查看目录，member按目录填写。仅一份文档，不执行、不递归、不读包内视频或密码包。返回成员file_ref用于续读。",
        parameters=[
            param("file_ref", "原压缩包编号", required=True),
            param("member", "完整成员名，按inspect目录填写", required=True),
        ],
        platforms=["qq"],
        trusted_context=CONTRACT,
        timeout_ms=190000,
    )
    async def read_archive(self, file_ref="", member="", **kwargs):
        return await self.service.read_archive(kwargs.get("_host_context"), file_ref, member)


def create_plugin():
    return NekoFiles()
