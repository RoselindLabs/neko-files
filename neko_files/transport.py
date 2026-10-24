"""Pinned public downloads, written privately and committed only after completion."""

from pathlib import Path
from urllib.parse import urljoin, urlsplit

import asyncio
import ipaddress
import os
import socket
import uuid

import aiohttp

from .authorization import FileError
from .jobs import io_call, settle


def public_ip(value):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not (
        address.is_multicast or address.is_reserved or address.is_unspecified
    )


def validate_url(url):
    if not isinstance(url, str) or not 0 < len(url) <= 4096 or any(ord(c) < 33 for c in url):
        raise FileError("文件地址无效")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in ["http", "https"]
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in [None, 80, 443]
        ):
            raise ValueError()
    except ValueError:
        raise FileError("只接受标准端口的公网HTTP/HTTPS资源") from None
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname in ["localhost", "metadata.google.internal"] or hostname.endswith(
        (".local", ".internal", ".localhost")
    ):
        raise FileError("文件地址指向内网")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return parsed
    if not public_ip(str(address)):
        raise FileError("文件地址指向非公网")
    return parsed


class PublicResolver(aiohttp.resolver.ThreadedResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        rows = await super().resolve(host, port, family)
        if not rows or any(not public_ip(row["host"]) for row in rows):
            raise OSError("DNS地址不是公网")
        return rows


def local_addresses():
    values = {"127.0.0.1", "::1"}
    try:
        values.update(
            str(ipaddress.ip_address(row[4][0]))
            for row in socket.getaddrinfo(socket.gethostname(), None)
        )
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))  # Address selection only, no packet is sent.
            values.add(probe.getsockname()[0])
    except OSError:
        pass
    return values


async def download(url, destination, limit, expected_size=0):
    destination = Path(destination)
    partial = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".part")
    output = None
    owned = {"created": False}
    current = url
    try:
        blocked = await io_call(local_addresses)

        class Resolver(PublicResolver):
            async def resolve(self, host, port=0, family=socket.AF_INET):
                rows = await super().resolve(host, port, family)
                if any(str(ipaddress.ip_address(row["host"])) in blocked for row in rows):
                    raise OSError("DNS地址属于本机")
                return rows

        connector = aiohttp.TCPConnector(resolver=Resolver(), force_close=True)
        async with aiohttp.ClientSession(
            connector=connector,
            trust_env=False,
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=35),
        ) as client:
            for _ in range(4):
                parsed = validate_url(current)
                try:
                    if str(ipaddress.ip_address(parsed.hostname)) in blocked:
                        raise FileError("文件地址属于本机")
                except ValueError as exc:
                    if isinstance(exc, FileError):
                        raise
                async with client.get(current, allow_redirects=False) as response:
                    if response.status in [301, 302, 303, 307, 308]:
                        if not response.headers.get("Location"):
                            raise FileError("文件重定向无目标")
                        current = urljoin(current, response.headers["Location"])
                        continue
                    if response.status != 200:
                        raise FileError(f"文件下载失败 HTTP {response.status}")
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise FileError("不接受传输压缩的文件资源")
                    if response.content_length and response.content_length > limit:
                        raise FileError("文件超过大小限制")

                    def open_private():
                        file = partial.open("xb")
                        owned["created"] = True
                        try:
                            partial.chmod(0o600)
                        except BaseException:
                            file.close()
                            raise
                        return file

                    opening = asyncio.create_task(asyncio.to_thread(open_private))
                    try:
                        output = await asyncio.shield(opening)
                    except asyncio.CancelledError:
                        file = await settle(opening)
                        await io_call(file.close)
                        raise
                    total = 0
                    async for block in response.content.iter_chunked(65536):
                        total += len(block)
                        if total > limit:
                            raise FileError("文件实际大小超过限制")
                        await io_call(output.write, block)
                    if (
                        not total
                        or (
                            response.content_length is not None and total != response.content_length
                        )
                        or (expected_size and total != expected_size)
                    ):
                        raise FileError("文件内容为空或实际大小与资源信息不符，请刷新索引")

                    def finish(file):
                        file.flush()
                        os.fsync(file.fileno())
                        file.close()

                    await io_call(finish, output)
                    # Exclusive atomic publication: never clobber an existing complete source.
                    await io_call(os.link, partial, destination)
                    return
            raise FileError("文件重定向过多")
    finally:
        if output is not None:
            await io_call(output.close)
        if owned["created"]:
            await io_call(partial.unlink, missing_ok=True)
