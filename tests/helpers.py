from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from neko_files.config import Config
from neko_files.service import FileService


def envelope(session="a", group="123"):
    return {
        "contract": "neko.tools.context.v1",
        "session_id": session,
        "stream_id": session,
        "platform": "qq",
        "group_id": group,
        "user_id": "111",
        "account_id": "",
    }


def scope(session="a", group="123", account="789"):
    return {"session": session, "account": account, "group": group, "user": "111"}


class ServiceFixture:
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config = Config()
        self.config.plugin.enabled = True
        self.config.access.group_ids = ["123", "456"]
        self.config.limits.reads_per_minute = 20
        self.ctx = SimpleNamespace(
            paths=SimpleNamespace(data_dir=self.directory.name),
            logger=Mock(),
            chat=SimpleNamespace(
                get_all_streams=AsyncMock(
                    return_value=[
                        {
                            "session_id": sid,
                            "stream_id": sid,
                            "platform": "qq",
                            "chat_type": "group",
                            "is_group_session": True,
                            "group_id": group,
                            "account_id": account,
                            "user_id": "111",
                        }
                        for sid, group, account in [("a", "123", "789"), ("b", "456", "987")]
                    ]
                )
            ),
            api=SimpleNamespace(call=AsyncMock()),
            llm=SimpleNamespace(
                generate=AsyncMock(side_effect=AssertionError("No real model calls"))
            ),
        )
        self.service = FileService()
        await self.service.start(self.ctx, self.config)
        self.addAsyncCleanup(self.service.stop)

    async def register(self, name="file.txt", data=b"synthetic", c=None):
        return await self.service.db(
            "register", c or scope(), {"file_id": name, "name": name, "size": len(data)}
        )

    async def cached(self, name="file.txt", data=b"synthetic"):
        item = await self.register(name, data)
        path = self.service.cache.path(item["ref"], Path(name).suffix)
        path.write_bytes(data)
        await self.service.cache.commit(item["ref"], Path(name).suffix)
        return item, path
