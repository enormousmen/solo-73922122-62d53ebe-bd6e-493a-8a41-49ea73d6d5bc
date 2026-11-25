# 离线 ext4 镜像扩容 API

两台小型虚拟机的磁盘以**未挂载的 ext4 镜像文件**形式存在。值班员在共享配额内按整 MiB
提交目标容量，API 用 e2fsprogs 离线（不 mount、不占用 loop 设备）完成扩容，并报告镜像
长度与 ext4 容量两个数字。

- `POST /disks/{disk1|disk2}/resize` `{"target_mib": 80}`
- `GET /disks` / `GET /disks/{disk}`
- `GET /disks/{disk}/jobs`
- `GET /healthz`

## 行为约定

| 要求 | 实现 |
| --- | --- |
| 预置两份含文件的未挂载 ext4 镜像 | 构建镜像时由 `mkfs.ext4 -d seed/<disk>` 生成，`seed/disk1`、`seed/disk2` 里各含 `readme.txt`、`payload.bin` 等文件；文件系统从不挂载 |
| 共用可配置总配额 | `DISK_QUOTA_MIB`，按镜像文件**长度**计费 |
| 按镜像号提交整 MiB 目标容量 | 请求体只有 `target_mib`，必须是整数且 ≥ 16；磁盘号只接受 `disk1`/`disk2`，路径固定，调用方无法指定任意文件 |
| 查询分别显示镜像长度和 ext4 容量 | 每个盘同时返回 `image_length_mib`（`stat` 得到的文件长度）和 `ext4_capacity_mib`（`dumpe2fs -h` 的 block count × block size） |
| 每盘一次只受理一单 | 在途时同盘再次提交 → `409 disk_busy`（同一目标且上一进程已退出时例外，见“续做”） |
| 额度够时两盘可同时扩 | 每个盘独立 worker；e2fsprogs 只在**同一镜像**上串行（按路径加锁），不同镜像并行 |
| 在途按目标长度计，其余按实际长度计 | 计费取 `max(目标, 当前长度)`；快照与准入都在同一把 async 锁内完成 |
| 额度不足就拒绝，两种容量保持原样 | 准入在任何写操作**之前**判定；拒绝时 409 `quota_exceeded`，不 truncate、不调 e2fsprogs |
| ext4 扩完才报成功 | `POST` 在 worker 结束后才返回；只有 `resize2fs` 返回 0 且重新读回的 ext4 容量 ≥ 目标才回 200，否则 500 `resize_incomplete` |
| 原文件须保留 | 只做 `truncate` 变长 + `resize2fs`，不写数据区；测试逐文件比对 sha256 |
| 中断续做 | 见下节 |

## 中断与续做

1. 受理时先把 job 行与目标长度写入 SQLite（`synchronous=FULL`），**再**动镜像文件。
2. 随后 `truncate` 拉长文件并 `fsync`，然后 `e2fsck -f -y`、`resize2fs`。
3. 若在“文件已拉长、ext4 未扩”之间进程消失：重启后 `disk` 行的 pending 标记仍在，
   该盘按目标长度计费（此时文件实际就是这个长度），`GET /disks` 显示
   `ext4_capacity_mib` 仍是旧值、`in_flight=true`。
4. 用**同一目标**再次 `POST` → 复用原 job id，`resumed=true`，只补跑 `resize2fs`。
   用**不同目标**提交 → `409 disk_busy`，先完成或续做原来的目标。
5. 若崩溃发生在 ext4 已扩完之后，重启只做账务收尾（标记 job 成功、释放 pending），
   不会重复扩容。
6. 若崩溃发生在文件拉长之前，则没有任何实际改动，重启直接释放这笔预占。

## 运行

### Docker Compose

```bash
cp .env.example .env          # 按需改配额
docker compose up --build     # 首次启动自动生成两份 32 MiB 镜像
curl -s localhost:8000/disks | python3 -m json.tool
```

镜像与状态分别放在命名卷 `disk-images`、`disk-state` 中；重启不会重建已存在的镜像。
配额等参数通过环境变量传入（`.env` 或 `docker-compose.yml`）：

| 变量 | 默认 | 含义 |
| --- | --- | --- |
| `DISK_QUOTA_MIB` | 160 | 共享配额（MiB） |
| `DISK_IMAGE_MIB` | 32 | 首次生成的镜像大小 |
| `DISK_EXTEND_DELAY_MS` | 0 | 在拉长文件前人为等待，便于观察“在途”状态 |
| `DISK_CRASH_AFTER_EXTEND` | 空 | 指定盘名后，该盘在拉长文件后立刻 `_exit`，用于演示中断 |
| `DISK_API_KEY` | 空 | 设置后除 `/healthz` 外都要求 `X-API-Key` |
| `DISK_COMMAND_TIMEOUT_SEC` | 300 | 单条 e2fsprogs 命令超时 |

### 不用 Docker

```bash
python3 -m pip install -r requirements.txt
DATA_DIR=./data ./scripts/make-images.sh
DISK_QUOTA_MIB=160 DISK_DATA_DIR=./data DISK_STATE_DB=./state/state.db \
  python3 scripts/serve.py --port 8000
```

`scripts/serve.py` 是开发/演示用的启动器（stdin 写 `stop` 或 `--exit-with-parent` 即可
优雅退出），生产等价命令是
`uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000`。

## 演示请求

两种场景都可一键复现，详见 [docs/DEMO.md](docs/DEMO.md)：

```bash
./scripts/demo-quota.sh          # 两盘争额度
./scripts/demo-crash-resume.sh   # 中断 + 重启续做
```

## 测试

```bash
python3 tests/unit.py            # 纯逻辑：计费、准入、恢复（假 ext4）
python3 -I tests/e2e.py          # 真实镜像 + 真实 e2fsprogs + 真实进程崩溃
```

`tests/e2e.py` 使用临时目录、随机端口，跑完自动清理。

## 目录

```
app/main.py              FastAPI 应用、配额与 e2fsprogs 调用
docker/entrypoint.sh     首次启动生成镜像，之后只复用
scripts/make-images.sh   本地生成镜像
scripts/serve.py         开发/演示启动器
scripts/inspect-image.py 不挂载地列出并哈希镜像内文件
scripts/demo-*.sh        两套演示脚本
tests/unit.py, tests/e2e.py
seed/                    预置进镜像的文件
docs/DEMO.md             演示记录与实测输出
```
