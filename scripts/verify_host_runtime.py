"""Actual fork Runner activation/RPC/reload/unload against an isolated instance."""

from pathlib import Path
from tempfile import TemporaryDirectory
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import argparse
import asyncio
import json
import importlib.metadata
import os
import shutil
import sys


async def verify(args):
    root = Path(__file__).resolve().parents[1]
    args.host_source = args.host_source.resolve()
    if args.report:
        args.report = args.report.resolve()
    if args.extra_site_packages:
        args.extra_site_packages = args.extra_site_packages.resolve()
    with ExitStack() as cleanup:
        directory = cleanup.enter_context(TemporaryDirectory(prefix="files-runner-", dir=args.temp_dir))
        instance = Path(directory).resolve()
        previous_cwd = Path.cwd()
        os.chdir(instance)
        cleanup.callback(os.chdir, previous_cwd)
        os.environ.update(
            MAIBOT_INSTANCE_DIR=str(instance), MAIBOT_LOCALE="zh-CN", MAIBOT_HOST_VERSION=args.host_version
        )
        if args.extra_site_packages:
            sys.path.append(str(args.extra_site_packages.resolve()))
        sys.path.insert(0, str(args.host_source.resolve()))
        sys.path.insert(0, str(args.host_source.resolve() / "scripts"))
        import neko_pytest_guard  # noqa: F401 -- audit guard, not a pytest invocation
        from src.plugin_runtime.runner.runner_main import PluginRunner, PluginActivationStatus
        from src.plugin_runtime.runner import runner_main
        from src.plugin_runtime import ENV_HOST_VERSION
        from src.plugin_runtime.protocol.envelope import Envelope
        from src.plugin_runtime.component_query import ComponentQueryService
        from src.plugin_runtime.host.authorization import AuthorizationManager
        from src.common.database.database import engine
        from src.common.logger import close_handlers

        cleanup.callback(engine.dispose)
        cleanup.callback(close_handlers)
        assert Path(runner_main.__file__).resolve().is_relative_to(args.host_source.resolve())
        assert (
            Path(ComponentQueryService._build_trusted_tool_context.__code__.co_filename)
            .resolve()
            .is_relative_to(args.host_source.resolve())
        ), "Do not fall back to another host checkout"

        os.environ[ENV_HOST_VERSION] = args.host_version
        assert runner_main._PROJECT_ROOT.resolve() == instance, "Refuse real host paths"
        target = instance / "plugins/neko-files"
        target.mkdir(parents=True)
        for name in ["plugin.py", "_manifest.json"]:
            shutil.copyfile(root / name, target / name)
        shutil.copytree(
            root / "neko_files", target / "neko_files", ignore=shutil.ignore_patterns("__pycache__")
        )
        (target / "config.toml").write_text(
            '[plugin]\nenabled=true\nconfig_version="1.0.0"\n[access]\ngroup_ids=["123"]\n',
            encoding="utf-8",
        )
        runner = PluginRunner(
            host_address="unix:///unused-files-test.sock",
            session_token="synthetic",
            plugin_dirs=[str(target.parent)],
        )
        registrations = []
        capabilities = []
        permissions = AuthorizationManager()
        requested = json.loads((root / "_manifest.json").read_text(encoding="utf-8"))["capabilities"]
        permissions.register_plugin("neko.files", requested)

        async def rpc(method, **kwargs):
            if method == "cap.call":
                name = kwargs["payload"]["capability"]
                capabilities.append(name)
                allowed, reason = permissions.check_capability("neko.files", name)
                if not allowed:
                    raise PermissionError(reason)
                assert name in ["chat.get_all_streams", "message.get_by_id"], "No QQ/model capability calls allowed"
                if name == "message.get_by_id":
                    query = kwargs["payload"]["args"]
                    assert query["chat_id"] == "a" and not query["include_binary_data"]
                    assert query["message_id"] == "1001"
                    message = {
                        "message_id": "1001", "session_id": "a", "platform": "qq", "account_id": "789",
                        "message_info": {"group_info": {"group_id": "123"}, "user_info": {"user_id": "111"}},
                        "raw_message": [{"type": "file", "data": {"file_id": "raw-synthetic", "name": "raw.txt", "size": "4"}}],
                    }
                    return SimpleNamespace(error=None, payload={"result": {"success": True, "message": message}})
                rows = [
                    {
                        "session_id": sid,
                        "stream_id": sid,
                        "platform": "qq",
                        "chat_type": "group",
                        "is_group_session": True,
                        "group_id": group,
                        "account_id": "789",
                        "user_id": "111",
                    }
                    for sid, group in [("a", "123"), ("b", "456")]
                ]
                return SimpleNamespace(error=None, payload={"result": rows})
            assert method in [
                "plugin.bootstrap",
                "plugin.register_components",
                "plugin.unregister",
            ], method
            if method == "plugin.register_components":
                registrations.append(kwargs["payload"])
            return SimpleNamespace(error=None, payload={"accepted": True})

        runner._rpc_client.send_request = AsyncMock(side_effect=rpc)
        metas = runner._loader.discover_and_load(
            [str(target.parent)], extra_available={"maibot-team.snowluma-adapter": "1.1.5"}
        )
        assert not runner._loader.failed_plugins and len(metas) == 1, runner._loader.failed_plugins
        meta = metas[0]
        assert runner._prepare_plugin_activation(meta) is None and not meta.config_error, (
            meta.config_error
        )
        status = await runner._activate_plugin(meta)
        assert status == PluginActivationStatus.LOADED and meta.instance.ready is True
        try:
            assert meta.instance.config.plugin.config_version == "1.0.0"
            tools = [
                item for item in registrations[0]["components"] if item["component_type"] == "TOOL"
            ]
            assert len(tools) == 4 and all(
                item["metadata"]["trusted_context"] == "neko.tools.context.v1" for item in tools
            )
            bad = runner._normalize_plugin_config(meta.instance, {"plugin": {"enabled": True}})
            assert bad.config_error, "Missing version must not look business-ready"
            c = {"session": "a", "account": "789", "group": "123", "user": "111"}
            record = await meta.instance.service.db(
                "register", c, {"file_id": "synthetic", "name": "safe.txt", "size": 4}
            )
            entry = SimpleNamespace(
                invoke_method="plugin.invoke_tool",
                metadata=next(t["metadata"] for t in tools if t["name"] == "neko_files_list"),
            )

            async def invoke(sid, group, **arguments):
                context = SimpleNamespace(
                    session_id=sid,
                    stream_id=sid,
                    platform="qq",
                    group_id=group,
                    user_id="111",
                    metadata={},
                )
                payload = ComponentQueryService._build_tool_invocation_payload(
                    entry,
                    SimpleNamespace(arguments={**arguments, "_host_context": {"session_id": "forged"}}),
                    context,
                )
                request = Envelope(
                    request_id=1,
                    message_type="request",
                    method="plugin.invoke_tool",
                    plugin_id="neko.files",
                    payload={"component_name": "neko_files_list", "args": payload},
                )
                response = await runner._handle_invoke(request)
                assert response.error is None
                return response.payload["result"]

            accepted = await invoke("a", "123")
            denied = await invoke("b", "456")
            assert accepted["success"] and accepted["files"][0]["ref"] == record["ref"]
            assert not denied["success"] and record["ref"] not in str(denied)
            sourced = await invoke("a", "123", message_id="1001")
            assert sourced["success"] and sourced["files"][0]["source_verified"]
            assert sourced["files"][0]["size"] == 4
            permissions.register_plugin("neko.files", [name for name in requested if name != "message.get_by_id"])
            blocked = await invoke("a", "123", message_id="1001")
            assert not blocked["success"], "Missing message capability must fail closed"
            permissions.register_plugin("neko.files", requested)
            for enabled in [False, True]:
                config = {
                    "plugin": {"enabled": enabled, "config_version": "1.0.0"},
                    "access": {"group_ids": ["123"]},
                }
                request = Envelope(
                    request_id=2,
                    message_type="request",
                    method="plugin.config_updated",
                    plugin_id="neko.files",
                    payload={
                        "plugin_id": "neko.files",
                        "config_scope": "self",
                        "config_version": "synthetic",
                        "config_data": config,
                    },
                )
                response = await runner._handle_config_updated(request)
                assert response.error is None, response.error
                assert meta.instance.ready is enabled
            result = {
                "real_loader": True,
                "business_activation": True,
                "tool_count": 4,
                "trusted_host_to_runner_rpc": True,
                "cross_scope_denied": True,
                "normalized_message_source_and_permission_denial": True,
                "host_version": args.host_version,
                "sdk_version": importlib.metadata.version("maibot-plugin-sdk"),
                "config_reload_disabled_then_enabled": True,
                "missing_version_rejected": True,
                "instance_isolated": True,
                "synthetic_capabilities": sorted(set(capabilities)),
                "qq_sends": 0,
                "model_calls": 0,
            }
        finally:
            await runner._invoke_plugin_on_unload(meta)
            assert not meta.instance.ready
        result["unloaded"] = True
    if args.report:
        args.report.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host-source", type=Path, required=True)
    parser.add_argument("--host-version", default="1.3.15")
    parser.add_argument("--temp-dir", type=Path)
    parser.add_argument("--extra-site-packages", type=Path)
    parser.add_argument("--report", type=Path)
    asyncio.run(verify(parser.parse_args()))
