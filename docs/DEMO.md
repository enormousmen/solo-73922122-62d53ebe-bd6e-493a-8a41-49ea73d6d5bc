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
