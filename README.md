# Neko Files 0.2.3 — local repair, not activated

按需读取独立授权的 QQ 群文件/文档和一份压缩包成员。默认关闭、空名单拒绝，
不自动下载、不发送聊天消息、不自动 OCR。当前是本地修复工作区，没有新建公开插件仓库或 Release。

## 配套宿主与配置

- 当前验证基线为 **MaiBot 1.3.15 + SDK 2.16.0**，需我们的维护 fork。
- 四个工具通过 SDK metadata `trusted_context="neko.tools.context.v1"` 显式申请只读宿主身份。
  fork 仅为 opt-in 工具覆盖模型同名 `_host_context`；其他工具/旧参数行为不变。
  Focus 使用已路由目标，不能借别群锚点账号。无上下文拒绝，不把模型自填信封当权限。
- `plugin.config_version="1.0.0"` 在默认模型及示例中均存在；缺版本的旧配置仍被宿主拒绝，
  不从失败备份自动恢复配置，不复制其他插件或朋友的授权名单。
- `access.group_ids/user_ids/account_id` 独立维护；群成员不必逐个加私聊名单。
  读取资源前核对适配器实际 `get_login_info`，不以传账号参数冒充多账号路由。
- `group_catalog` 和 `ocr` 独立默认关闭。除既有 `group_upload` 元数据登记外，0.2.2 起也从
  消息文本的 `[文件] … 链接: <url>` 标记登记文件附件（适配器压平后的真实可用结构；本机
  `additional_config` 不携带附件结构）。聊天原生视频由 Neko Video 的 `neko_video_chat` 读取，
  不能传任意本地路径绕授权。

## 工具

1. `neko_files_list`：当前会话索引；明确授权后用 `include_group`/`folder_id` 查目录。
   每页20项，5分钟 scope-bound cursor，不重查QQ；来源最多500文件/目录，超出明确标记。
2. `neko_files_inspect(file_ref)`：结构、有限预览、压缩包目录；不是全文。
3. `neko_files_read(file_ref,...)`：页/行/段范围、工作表、关键词与字符续读。
   工作表用独立 `sheet`/`row` 字段匹配，不再按显示标签前缀混表。
4. `neko_files_archive_read(file_ref,member)`：只提取目录中明确的一份文档，不递归、不执行、不读包内视频/密码包。

## 修复边界

- 下载只接受适配器返回的80/443公网HTTP/HTTPS；逐跳/DNS/本机地址和字节限制保持，
  不接受模型任意URL或本地路径，不绕TLS、不使用环境代理/Cookie或传输解压。
- 先写随机私有 `.part`，核完整长度并fsync，再以无覆盖原子方式发布最终数据。
  只有带完整 SHA256/大小提交记录的 raw 才可使用；硬退出半文件/无标记旧缓存不能当成功来源。
- 每文件互斥，不同文件可并发；短锁进行额度预留，处理中pin防驱逐。
  保留单文件20MiB默认/40MiB上限、256MiB缓存默认/128MiB磁盘余量、最多2执行/8任务。
  预留采用保守上界，额度不足明确拒绝，不能通过删锁获得无界并发。
- 文件hash/JSON/fsync等IO在线程完成后才释放所有权；取消时等自己的IO/子进程收尾。
  parser创建窗口也保留handle并kill/reap；协作取消不承诺任意坏磁盘/系统调用的硬实时期限。
- Linux解析子进程保持1GiB地址空间/30秒CPU/40MiB文件/有限输出，**不是文件系统/网络沙箱**。
  不执行宏/公式/代码，保留PDF/Office/压缩包限额，外部资料与预览/解析截断/输出截断分开。
- OCR仍须管理员开启且显式单页；成功缓存一小时，字符续读不再调用模型。
  持久尝试准入包含未知/取消，缓存驱逐或重载不会清掉防重记录；不自动重试未知请求。

## 验证

```console
uv sync --locked
uv run --no-sync python scripts/check.py
uv run --no-sync python scripts/package.py
```

`scripts/verify_host_runtime.py --host-source <fork目录> --report <报告路径>` 在全新临时实例中使用真实
宿主loader/config/组件/RPC/重载/卸载。只模拟IPC，唯一允许的业务capability为合成会话查询；
不连接真实Host/QQ/模型。若宿主开发环境不含解析依赖，可通过 `--extra-site-packages` 指向仅含解析库的临时目录。
不能指向生产实例。Windows显式跳过真正的Linux资源限制子进程测试，Linux必须实际通过。

新测试不是丢失的原69项。包是显式成员白名单，不含真实config/data/凭据/恢复元信息，
隔离解压后真实SDK导入所有模块、配置及四工具发现，缺文件/私密成员/摘要不符都拒绝。

原始13文件快照仍保留在独立 `neko-files-review/original`，未改。
当前代码整理了SDK适配、文件工作流、OCR、缓存、IO/解析作业边界，不是全量重写解析器。
来源和公开发布许可仍待核对，见 [NOTICE.md](NOTICE.md)。本轮源代码修复不授权部署、启用、宿主重启或真实模型/QQ验收。
