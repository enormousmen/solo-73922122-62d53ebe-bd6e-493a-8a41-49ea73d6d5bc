# 演示请求与实测输出

以下命令都在项目根目录执行。两次演示都**不需要 Docker**：脚本会在临时目录里生成两份
32 MiB 的 ext4 镜像，用 `scripts/serve.py` 起服务，结束后自动清理（加 `WORK=...` 可保留）。
`docker compose` 下等价请求只是把地址换成 `localhost:8000`，请求体完全相同。

---

## 场景 A：两盘争额度

```bash
./scripts/demo-quota.sh
```

配额 160 MiB，初始两盘各 32 MiB（已用 64 MiB）。脚本给 `DISK_EXTEND_DELAY_MS=1500`，
让“在途”窗口足够长，能在任务完成前查询状态。

### 请求序列

```bash
# 1) 初始状态：两张盘各有镜像长度与 ext4 容量
curl -s localhost:8000/disks

# 2) disk1 目标 80 MiB，在它跑的时候再提交 disk2 目标 64 MiB
curl -s -X POST localhost:8000/disks/disk1/resize \
     -H 'content-type: application/json' -d '{"target_mib": 80}' &
sleep 0.3
curl -s localhost:8000/disks                     # disk1 在途：charge=80，length 仍 32
curl -s -X POST localhost:8000/disks/disk1/resize \
     -H 'content-type: application/json' -d '{"target_mib": 72}'   # 409 disk_busy
curl -s -X POST localhost:8000/disks/disk2/resize \
     -H 'content-type: application/json' -d '{"target_mib": 64}' &
wait

# 3) 两盘都完成后，已用 144/160，只剩 16 MiB —— 96 MiB 的申请必须被拒
curl -s -X POST localhost:8000/disks/disk2/resize \
     -H 'content-type: application/json' -d '{"target_mib": 96}'

# 4) 恰好放得下的申请可以成功
curl -s -X POST localhost:8000/disks/disk2/resize \
     -H 'content-type: application/json' -d '{"target_mib": 80}'
```

### 实测输出（节选）

```
== 1) initial state ==
  quota 160 MiB, used 64.0 MiB, free 96.0 MiB
  disk1: image 32 MiB, ext4 32 MiB, charged 32 MiB, in_flight False
  disk2: image 32 MiB, ext4 32 MiB, charged 32 MiB, in_flight False

== 2) disk1 -> 80 MiB and disk2 -> 64 MiB at the same time ==
-- while disk1's job is in flight:
  quota 160 MiB, used 112.0 MiB, free 48.0 MiB
  disk1: image 32 MiB, ext4 32 MiB, charged 80 MiB, in_flight True   <-- 按目标计费
  disk2: image 32 MiB, ext4 32 MiB, charged 32 MiB, in_flight False
-- a second job for the same disk while one is running:
HTTP 409
{
    "detail": {
        "error": "disk_busy",
        "message": "disk1 already has job 2fcb16164c06411fa52cac181d14f643 in flight",
        "pending_target_mib": 80
    }
}
-- both jobs in flight:
  quota 160 MiB, used 144.0 MiB, free 16.0 MiB
  disk1: image 32 MiB, ext4 32 MiB, charged 80 MiB, in_flight True
  disk2: image 32 MiB, ext4 32 MiB, charged 64 MiB, in_flight True   <-- 两盘同时扩
-- finished:
  disk1: image 80 MiB, ext4 80 MiB ...
  disk2: image 64 MiB, ext4 64 MiB ...

== 3) a request that does not fit: disk2 -> 96 MiB ==
-- used is 144/160, so only 16 MiB are free:
HTTP 409
{
    "detail": {
        "error": "quota_exceeded",
        "message": "request would exceed the shared quota",
        "quota_mib": 160,
        "used_mib": 144.0,
        "requested_target_mib": 96,
        "available_mib": 80
    }
}
-- both capacities are unchanged:
  disk1: image 80 MiB, ext4 80 MiB, charged 80 MiB, in_flight False
  disk2: image 64 MiB, ext4 64 MiB, charged 64 MiB, in_flight False

== 4) a request that fits exactly: disk2 -> 80 MiB ==
HTTP 200
...
    "status": "succeeded",
    "resumed": false,
    "target_mib": 80,
    "result": { "image_length_mib": 80, "ext4_capacity_mib": 80, ... }

== 5) final state and content check ==
  quota 160 MiB, used 160.0 MiB, free 0.0 MiB
  disk1: image 80 MiB, ext4 80 MiB ...
  disk2: image 80 MiB, ext4 80 MiB ...
--- /tmp/ext4-demo-quota-.../data/disk1.ext4
  /data/payload.bin  98304 bytes  e72ddff4…
  /docs/notes.md     178 bytes    86c64c2e…
  /readme.txt        101 bytes    8a1ef145…
--- /tmp/ext4-demo-quota-.../data/disk2.ext4
  /conf/app.conf     60 bytes    cae80adb…
  /data/payload.bin  98304 bytes  1cc86846…
  /readme.txt        112 bytes    4a09fc83…
```

---

## 场景 B：拉长后中断，重启续做

```bash
./scripts/demo-crash-resume.sh
```

脚本以 `DISK_CRASH_AFTER_EXTEND=disk1` 启动，`disk1` 在 `truncate` 到 96 MiB、`fsync`
之后立刻 `_exit(137)`，`resize2fs` 根本不会执行 —— 等价于两次系统调用之间被 `kill -9`。

### 请求序列

```bash
# 1) 触发中断（客户端会看到连接断开）
curl -X POST localhost:8000/disks/disk1/resize \
     -H 'content-type: application/json' -d '{"target_mib": 96}'

# 2) 直接看文件：长度 96 MiB，ext4 仍是 32 MiB
stat -c %s /data/disk1.ext4
dumpe2fs -h /data/disk1.ext4 | grep -E 'Block count|Block size'

# 3) 去掉故障注入重启，查询：disk1 仍按 96 MiB 计费且 in_flight
docker compose up -d          # 或再次运行 scripts/serve.py
curl -s localhost:8000/disks

# 4) 换目标被拒
curl -X POST localhost:8000/disks/disk1/resize \
     -H 'content-type: application/json' -d '{"target_mib": 112}'

# 5) 同一目标续做
curl -X POST localhost:8000/disks/disk1/resize \
     -H 'content-type: application/json' -d '{"target_mib": 96}'
```

### 实测输出

```
== 1) start with fault injection: die after extending disk1 ==
curl: (52) Empty reply from server
(connection died, as expected)

== 2) inspect the image directly (never mounted) ==
image length: 96 MiB
ext4 capacity: 32 MiB            <-- 只完成了“加长文件”，没扩文件系统

== 3) restart without fault injection; quota already charges 96 MiB ==
used/quota MiB: 128.0 / 160
  disk1 length 96 MiB ext4 32 MiB pending 96 in_flight True
  disk2 length 32 MiB ext4 32 MiB pending None in_flight False
-- a different target is refused:
{"detail":{"error":"disk_busy","message":"disk1 has unfinished job e9c650b1...","pending_target_mib":96}}

== 4) resume with the same target ==
{
    "job_id": "e9c650b1448547fc9b727d4cb3ce4e9c",     <-- 沿用原 job id
    "disk": "disk1",
    "status": "succeeded",
    "resumed": true,
    "target_mib": 96,
    "result": {
        "image_length_mib": 96,
        "ext4_capacity_mib": 96,
        "pending_job_id": null,
        "in_flight": false
    }
}

== 5) verify content and final state ==
--- .../data/disk1.ext4
  /data/payload.bin  98304 bytes  e72ddff4…
  /docs/notes.md     178 bytes    86c64c2e…
  /readme.txt        101 bytes    8a1ef145…
```

重启后 96 MiB 的占用来自**磁盘上真实的文件长度**与持久化的 pending 目标长度的较大者，
不会因为“服务没做完”而把额度退回去；续做只补跑 `e2fsck`/`resize2fs`，原文件哈希不变。

---

## 场景 C：主超级块魔数损坏 + 备用块离线修复

```bash
./scripts/demo-repair.sh
```

脚本会在临时目录里生成一张**带备用超级块**的 32 MiB / 4 KiB 镜像（`mkfs -g 4096`，
备用块在 `4096`；默认几何的 32 MiB 镜像只有一个块组、没有备份，所以故障样盘要单独
生成），只把主超级块偏移 1080 处的两个魔数字节 `53 ef` 改成 `00 00`，然后演示：
带坏盘启动 → 查询标故障且按文件长度占配额 → 修盘中本盘拒扩、disk2 照常扩到 64 MiB →
非法备用块号镜像不变 → 替换前硬退出 sha256 不变 → 重启重试成功、文件哈希不变。

### 故障样盘的两种生成方式

```bash
# 方式一：紧凑几何（32 MiB，备份超级块在 block 4096，省配额）
./scripts/make-fault-sample.sh /tmp/fault-data

# 方式二：标准几何（256 MiB，经典备份在 block 32768）
STANDARD=1 ./scripts/make-fault-sample.sh /tmp/fault-data

# 也可以只损坏一张已有镜像并自动扫描可用备份块：
python3 scripts/corrupt-superblock.py --json /path/to/disk1.ext4
```

### 请求序列

```bash
# 0) 起服务（本地）；Docker Compose 见下
DISK_QUOTA_MIB=160 DISK_DATA_DIR=/tmp/fault-data \
  DISK_STATE_DB=/tmp/fault-data/state.db \
  DISK_REPAIR_DELAY_MS=1500 \
  python3 scripts/serve.py --port 8000

# 1) 服务带着坏盘启动；disk1 healthy=false / fault=superblock_magic_corrupt，
#    仍按 32 MiB 文件长度计费
curl -s localhost:8000/disks | python3 scripts/show-status.py

# 2) 发起修复（已知备用块号 4096；标准 256M 样盘用 32768）
curl -s -X POST localhost:8000/disks/disk1/repair \
     -H 'content-type: application/json' \
     -d '{"backup_superblock": 4096}' &
sleep 0.5

# 3) 修盘期间：本盘扩容 409 disk_busy(repair_in_progress)
curl -s -X POST localhost:8000/disks/disk1/resize \
     -H 'content-type: application/json' -d '{"target_mib": 48}'

# 4) 另一盘照常按配额扩容并完成（32 + 64 <= 160）
curl -s -X POST localhost:8000/disks/disk2/resize \
     -H 'content-type: application/json' -d '{"target_mib": 64}'

# 5) 非法备用块号 -> 422，原镜像逐字节不变
curl -s -X POST localhost:8000/disks/disk1/repair \
     -H 'content-type: application/json' -d '{"backup_superblock": 999999}'

# 6) 替换前崩溃：带 DISK_CRASH_BEFORE_REPLACE=disk1 重启后再发修复，
#    服务在只读检查通过后、os.replace 前 _exit(137)；sha256 不变。
#    去掉该变量重启，再次 POST 同一修复即可成功。
```

### 实测输出（本机 e2fsprogs 1.47.0）

```
== 1) service starts with the bad disk; GET /disks marks the fault ==
  quota 160 MiB, used 64.0 MiB, free 96.0 MiB
  disk1: image 32 MiB, ext4 0 MiB, charged 32 MiB, in_flight False healthy=False  [FAULT=superblock_magic_corrupt]
  disk2: image 32 MiB, ext4 32 MiB, charged 32 MiB, in_flight False healthy=True

== 2) repair ... concurrently probe expansions ==
-- disk1 expansion while its repair runs:
HTTP 409
  disk_busy repair_in_progress
-- disk2 expansion to 64 MiB at the same time:
HTTP 200
  succeeded ext4_mib= 64
-- repair result:
  succeeded backup 4096 len_mib 32 healthy True ext4_mib 32

== 3) invalid backup number changes nothing ==
HTTP 422
  invalid_backup_superblock
  PASS: original image byte-identical

== 4) hard exit before replacement also keeps every byte ==
  api exit status: 137 (137 = fault injection before replace)
  PASS: sha256 unchanged by the crashed repair

== 5) restart without the hook and retry ==
  succeeded healthy True length_mib 32 ext4_mib 32

== 6) original files are still readable ==
  /data/payload.bin  98304 bytes  e72ddff4…
  /docs/notes.md     178 bytes    86c64c2e…
  /readme.txt        101 bytes    8a1ef145…
```

自动化版本见 `tests/e2e.py` 的 scenario 4–7（故障标记/配额/并发、参数与块大小
守卫、崩溃重试、256 MiB/32768 标准几何）。

### Docker Compose 下生成故障样盘并修复

Compose 的镜像在命名卷 `disk-images` 里，首次 `up` 生成的 32 MiB 镜像同样没有
备用块，所以先把 disk1 换成带备份的几何，或直接做“仅魔数损坏 + 32768 不可用”之外
的标准大镜像实验。最直接的做法是用容器内自带的脚本操作卷内文件：

```bash
docker compose up -d --build

# 方式 A（推荐）：在卷内把 disk1 重做成 256 MiB 标准几何并损坏主魔数。
# 先停服务，避免在 mkfs 时并发读；卷由 diskresize 服务独占使用。
docker compose stop diskresize
docker compose run --rm --no-deps --entrypoint bash diskresize -c '
  mkfs.ext4 -q -F -b 4096 -L disk1 -d /srv/seed/disk1 /data/disk1.ext4 256M &&
  truncate -s 256M /data/disk1.ext4 &&
  e2fsck -f -y /data/disk1.ext4 >/dev/null &&
  python3 /srv/scripts/corrupt-superblock.py /data/disk1.ext4'
docker compose up -d

# 查询：disk1 healthy=false / fault=superblock_magic_corrupt，按 256 MiB 占配额
curl -s localhost:8000/disks | python3 -m json.tool

# 修复（标准几何的备用超级块号）
curl -s -X POST localhost:8000/disks/disk1/repair \
     -H 'content-type: application/json' \
     -d '{"backup_superblock": 32768}'
```

> 配额提醒：256 MiB 的故障盘 + 另一张 32 MiB 盘，默认 `DISK_QUOTA_MIB=160`
> 会让总额显示超限（只影响新扩容准入，不影响查询与修复）。要同时演示 disk2
> 扩容，请在 `.env` 设 `DISK_QUOTA_MIB=320`。若用紧凑几何
> （`mkfs.ext4 -b 4096 -g 4096 ... 32M`，备份号 `4096`），默认配额即可。

演示“替换前崩溃”的 Compose 等价流程：

```bash
# .env 中设置 DISK_CRASH_BEFORE_REPLACE=disk1 后重建容器
DISK_CRASH_BEFORE_REPLACE=disk1 docker compose up -d --force-recreate
curl -X POST localhost:8000/disks/disk1/repair \
     -H 'content-type: application/json' -d '{"backup_superblock": 32768}'
# 容器按 restart 策略重启后仍带该变量会再次注入；确认原镜像 sha256 未变后，
# 清空变量并强制重建，再 POST 同一修复即可成功。
```

---

## 不启动脚本、只用 Compose 的等价做法

```bash
docker compose up -d --build

# 场景 B 的中断：先带故障注入启动，触发后再正常启动
DISK_CRASH_AFTER_EXTEND=disk1 docker compose up -d --force-recreate
curl -X POST localhost:8000/disks/disk1/resize -H 'content-type: application/json' -d '{"target_mib": 96}'
docker compose up -d --force-recreate        # 变量来自 .env，这里就是空
curl -s localhost:8000/disks
curl -X POST localhost:8000/disks/disk1/resize -H 'content-type: application/json' -d '{"target_mib": 96}'
```

`DISK_EXTEND_DELAY_MS=2000` 同理可在 Compose 下放大“在途”窗口，方便并发演示。
