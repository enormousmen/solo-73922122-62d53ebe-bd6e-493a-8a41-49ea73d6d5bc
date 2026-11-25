# 离线 ext4 镜像扩容 / 超级块修复 API

两台小型虚拟机的磁盘以**未挂载的 ext4 镜像文件**形式存在。值班员在共享配额内按整 MiB
提交目标容量，API 用 e2fsprogs 离线（不 mount、不占用 loop 设备）完成扩容，并报告镜像
长度与 ext4 容量两个数字。主超级块魔数损坏时，还可凭**已知的备用超级块号**发起离线修复：
服务先在临时副本上用备用块恢复并检查，通过只读主超级块检查后才原子替换原镜像。

- `POST /disks/{disk1|disk2}/resize` `{"target_mib": 80}`
- `POST /disks/{disk1|disk2}/repair` `{"backup_superblock": 32768}`（超级块修复）
- `GET /disks` / `GET /disks/{disk}`
- `GET /disks/{disk}/jobs` / `GET /disks/{disk}/repairs`
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

## 主超级块魔数损坏与离线修复

`POST /disks/{disk}/repair`，请求体给出已知的备用超级块块号（字段名兼容
`backup_superblock` / `backup_sb` / `superblock` 等，也支持 query 与裸整数 body）：

```bash
curl -s -X POST localhost:8000/disks/disk1/repair \
     -H 'content-type: application/json' \
     -d '{"backup_superblock": 32768}'
```

| 要求 | 实现 |
| --- | --- |
| 按盘号 + 已知备用超级块号发起 | 只接受 `disk1`/`disk2`；备用块号必须为正，并在镜像对应偏移处校验 ext4 魔数与 `s_log_block_size` 自洽 |
| 只处理 4 KiB 块大小 | 先读主超级块残留的 `s_log_block_size`，再用备用块自校验；非 4096（如 1 KiB 镜像的备份 8193）→ 422 `unsupported_block_size`，镜像不动 |
| 且没有未结扩容 | 该盘有持久化 pending 扩容或扩容 worker 在跑 → 409 `disk_busy` / `reason=unfinished_expansion` |
| 服务带坏盘也能启动 | 启动不再因主魔数缺失而报错；`GET /disks` 中该盘 `"healthy": false`、`"fault": "superblock_magic_corrupt"`（另附 `corrupted`/`state` 等别名） |
| 坏盘按文件长度占共享配额 | 坏盘读不到 ext4 容量（显示 0），但 `quota_charge_mib` 仍是 `stat` 到的真实文件长度，总额度口径不变 |
| 修盘期间本盘拒绝扩容 | repair worker 在跑时对该盘 `POST /resize` → 409 `disk_busy` / `reason=repair_in_progress` |
| 另一盘照常按配额判断 | 修复只锁本盘的暂存文件；另一盘的准入、配额与扩容完全不受影响，额度够就返回 200 并扩完 |
| 用主超级块做只读检查 | 先 `e2fsck -f -y -b <N> -B 4096 <暂存副本>` 从备份恢复主超级块，再跑**严格只读**的 `e2fsck -f -n <暂存副本>`（使用已恢复的主超级块），其返回码必须为 0 |
| 通过后才替换原镜像 | 校验通过后才 `os.replace(暂存, 原镜像)` 并 fsync 目录；同目录暂存保证原子替换 |
| 仍按原盘号查容量、长度不变、文件可读 | 成功响应与 `GET /disks/{disk}` 恢复 `ext4_capacity_mib`；worker 校验替换前后文件长度一致；debugfs/sha256 比对全部原有文件 |
| 失败/替换前退出逐字节保留 | 所有写操作只发生在 `.{disk}.repair-*.stage` 暂存文件；备用块非法、fsck 不过 → 422/500 且原镜像 sha256 不变；可用 `DISK_CRASH_BEFORE_REPLACE` 注入“检查已过、替换前 `_exit(137)`” |
| 重启后可重试 | 崩溃留下的暂存文件在下次启动时清理，repair job 标记为 interrupted/failed；原镜像仍是坏的，重启后用同一备用块号再次 POST 即可 |

注意：mkfs 默认几何下，很小的镜像可能**没有**备用超级块（例如 32 MiB / `-b 4096`
只有一个块组）。故障样盘可用更大的标准几何（256 MiB → 备份在 32768）或
`-g 4096` 的紧凑几何（32 MiB → 备份在 4096）生成，步骤见
[docs/DEMO.md](docs/DEMO.md#场景-c超级块魔数损坏与离线修复)。

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
| `DISK_REPAIR_DELAY_MS` | 0 | 修复任务开始时人为等待，便于观察“修盘中/本盘拒扩、另一盘照常” |
| `DISK_CRASH_BEFORE_REPLACE` | 空 | 指定盘名（或 `all`/`1`）后，修复在只读检查通过、原子替换前 `_exit(137)` |
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

两种扩容场景都可一键复现，详见 [docs/DEMO.md](docs/DEMO.md)：

```bash
./scripts/demo-quota.sh          # 两盘争额度
./scripts/demo-crash-resume.sh   # 中断 + 重启续做
./scripts/demo-repair.sh         # 超级块损坏、修复并发、逐字节保留与崩溃重试
```

## 测试

```bash
python3 tests/unit.py            # 纯逻辑：计费、准入、恢复、SB 解析（假 ext4）
python3 -I tests/e2e.py          # 真实镜像 + 真实 e2fsprogs + 真实进程崩溃
```

`tests/e2e.py` 使用临时目录、随机端口，跑完自动清理；当前 14 个 unit 与
56 项 e2e 检查全部通过（含 4 个超级块修复场景）。

## 目录

```
app/main.py                       FastAPI 应用、配额、e2fsprogs 与修复流程
docker/entrypoint.sh              首次启动生成镜像，之后只复用
scripts/make-images.sh            本地生成镜像
scripts/make-fault-sample.sh      生成带已知备用 SB 的魔数损坏样盘
scripts/corrupt-superblock.py     损坏/查看主魔数并扫描备用超级块
scripts/serve.py                  开发/演示启动器
scripts/inspect-image.py          不挂载地列出并哈希镜像内文件
scripts/demo-*.sh                 三套演示脚本
tests/unit.py, tests/e2e.py
seed/                             预置进镜像的文件
docs/DEMO.md                      演示记录与实测输出
```
