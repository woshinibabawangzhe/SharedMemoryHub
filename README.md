# 共享记忆中心（SharedMemoryHub）

服务基线v0.3.0；便携维护修订v0.1.0；Codex接入v0.1.1。个人自维护项目，受Mem0理念启发；当前服务不是直接部署mem0上游。

主模型提炼可读记忆；本地Qwen3-Embedding-0.6B编码向量；SQLite保存正文、修订、提案与状态；Docker Qdrant保存可重建索引。看板可管理正文、历史、锁定、软删、恢复及提案。共享中心供各客户端按身份接入，源码仓库不携带个人记忆或认证。

## 在本机使用

现有正式实例使用`runtime/codex`、8765端口及专用`smh-codex-memory`容器。目录名不代表只允许Codex接入。双击初始化、启动或停止入口，具体见[日常使用](使用与启停-1B-Codex-v0.1.0.md)。日常模型调用不会自动启动服务。

根目录`docker-compose.yml`保留旧实例用途，绑定`data/qdrant`；它不能代替正式实例的启停入口。不要用它启动另一个占用同一端口的空库。

## 换电脑

先读[换机部署与数据恢复](docs/换机部署与数据恢复-v0.1.0.md)，取得本仓库和独立私密数据备份，再按锁文件安装。当前锁文件适用Windows x64/Python3.13，CUDA构建不代表任意新电脑具备GPU运行条件；CPU须显式选择。

```powershell
py -3.13 -B scripts/portable_setup.py install --download-model
& ./.venv-0.3.0/Scripts/python.exe -X utf8 -B scripts/portable_setup.py prepare
```

安装会下载依赖和模型，全部留在本项目E盘区域；它不会改Windows/WSL/Docker全局设置。首次口令在本机终端输入，不进入命令参数或源码。恢复数据后按恢复说明初始化新认证，不复制旧机令牌和会话。

## 源码、记忆与Harness分别恢复

- 本仓库保存服务、看板、Schema、锁文件、脚本、测试及操作规范。
- Codex现役仓库保存`tools/shared-memory-hub`、全局主干与索引；实际依赖此服务。
- 记忆数据包单独保存SQLite与最新永久清除记录。备份默认未加密，只能放在受保护的私有位置或用户加密介质；不上传源码仓库。
- 模型由固定来源修订与`model.lock.json`核对；向量索引重建，旧凭据重新初始化。

维护时显式选择待存档文件，禁止整目录上传。`runtime/`、`data/`、`config/`、模型、依赖、缓存、私密导出、备份及历史运行回执均排除。历史设计与回执的本机链接不意味着它们全部包含在源码仓库。

验证分开报告：确定性测试、同机隔离恢复、桌面新会话和真正换机。当前版本与身份见`deployment-manifest.json`；全量复制覆盖与用户裁决见本机迁入回执。旧记忆机制在另行批准退役前继续保留。
