# SPDK Hot Upgrade

> **Version**: SPDK v26.09-pre + hot upgrade extension (commit `a8be7fce9`, branch `master_hot_replace`)
> **Updated**: 2026-07-03
> **Test Results**: Phase 12 — 3/3 PASSED, TSC IO interruption 57–133ms (target <200ms)

---

<!-- ================================================================ -->
<!--                    ENGLISH VERSION (FIRST)                       -->
<!-- ================================================================ -->

# English Version

## 1. Overview

SPDK Hot Upgrade allows upgrading a running SPDK Primary process to a new version **without interrupting IO service**. It leverages the DPDK multi-process mechanism: the new-version Secondary process starts and completes subsystem pre-initialization while the Primary is still serving IO, then switches IO responsibility through a state-machine-driven drain → suspend → takeover flow. The entire **IO interruption target is < 200ms**.

## 2. Core Features

| Feature | Description |
|---------|-------------|
| Parallel pre-init | Secondary starts and completes subsystem pre-init while Primary is still running, overlapping with Primary IO processing |
| State machine driven | 8-state atomic state machine; orchestration script and SPDK internals coordinate via state polling |
| bdev reconstruction | Primary saves bdev construction parameters; Secondary rebuilds bdevs via module callback `secondary_reconstruct` |
| vhost FD transfer | Guest memory FDs + rte_vhost connection state transferred between processes via SCM_RIGHTS |
| TSC timeline | 6 instrumentation points precisely measure per-phase IO interruption time |
| Automatic rollback | Any orchestration step failure triggers automatic rollback, restoring Primary operation |
| Single entry point | `spdk_hot_upgrade.sh` handles precheck → do → commit in one script |

## 3. Supported Versions & Subsystems

### 3.1 Version Compatibility

- Based on **SPDK v26.09-pre** (DPDK 26.03.0)
- Primary and Secondary must use compatible DPDK versions; precheck rejects cross-major-version upgrades
- State file structure changes invalidate old files (size check)

### 3.2 Supported Subsystems

| Subsystem | drain_io | suspend | secondary_pre_init | secondary_takeover |
|-----------|:--------:|:-------:|:------------------:|:------------------:|
| **iobuf** | — | — | ✓ (`spdk_iobuf_initialize`) | — |
| **accel** | — | — | ✓ (`spdk_accel_initialize`) | — |
| **bdev** | ✓ (no-op) | ✓ (save bdev params) | ✓ (`spdk_bdev_initialize` + reconstruct) | stub (reconstruction done in pre_init) |
| **vhost_blk** | ✓ | ✓ (extract FD + save device info) | ✓ (rebuild vhost devices) | ✓ (receive FD + restore connections) |
| **vhost_scsi** | ✓ | ✓ | stub | ✓ |

> **Topological order**: iobuf → accel → bdev (via `subsystem_sort()`). This ensures `g_modules_opc` is populated before `bdev_register` dereferences it.

### 3.3 bdev Modules with Reconstruction Support

| Module | Reconstruction function | Description |
|--------|------------------------|-------------|
| malloc | `malloc_secondary_reconstruct` | Rebuilds Malloc bdev from saved (name, block_size, num_blocks) |

> Other modules (e.g., NVMe) must implement the `secondary_reconstruct` callback to be rebuilt in the Secondary process.

## 4. Build

Hot upgrade is compiled by default — no extra configure options needed.

```bash
./configure
make -j$(nproc)
```

> **Note (Lima VM / some ARM64 environments)**: SPDK's configure script may fail to detect libuuid even when it is installed. If `CONFIG_HAVE_LIBUUID=n` after configure, manually edit the `CONFIG` file:
> ```bash
> sed -i 's/CONFIG_HAVE_LIBUUID=n/CONFIG_HAVE_LIBUUID=y/' CONFIG
> sed -i 's/CONFIG_HAVE_UUID_GENERATE_SHA1=n/CONFIG_HAVE_UUID_GENERATE_SHA1=y/' CONFIG
> make -j$(nproc)
> ```

Build artifacts:
- `build/bin/spdk_tgt` — SPDK target binary, linked with hot_upgrade library
- `build/lib/libspdk_hot_upgrade.so` — hot upgrade library

## 5. Enabling Hot Upgrade

Hot upgrade is enabled via **runtime parameters** — no build switch required.

### Primary Startup

Primary must use the following parameters to support subsequent hot upgrade:

```bash
build/bin/spdk_tgt \
  -m 0x1 \                          # CPU core mask (Secondary must inherit)
  -i 0 \                            # DPDK shm_id (multi-process shared memory ID, required)
  --base-virtaddr 0x200000000000 \  # DPDK base virtual address (recommended, ensures pointer validity)
  -r /var/tmp/spdk_hot_upgrade.sock # RPC socket path
```

| Parameter | Required | Description |
|-----------|:--------:|-------------|
| `-i, --shm-id <id>` | **Required** | DPDK shared memory ID; Secondary connects to Primary's hugepages via this |
| `--base-virtaddr <addr>` | Recommended | DPDK base virtual address; ensures bdev pointers remain valid across processes |
| `-m, --cpumask <mask>` | Required | CPU core mask; Secondary must use the same mask |
| `-r, --rpc-addr <path>` | Required | RPC socket path |

Secondary is started automatically by the orchestration script — **no manual execution needed**.

## 6. Usage

### 6.1 Single Entry Point (Recommended)

```bash
# Usage: spdk_hot_upgrade.sh <artifact> [options]
# artifact supports: tar package (.tar.gz) / directory / executable file

# Example: upgrade with a new-version tar package
scripts/hot_upgrade/spdk_hot_upgrade.sh ./new_spdk.tar.gz

# Example: specify socket + timeout + verbose output
scripts/hot_upgrade/spdk_hot_upgrade.sh ./new_spdk.tar.gz \
  --primary-sock /var/tmp/spdk_hot_upgrade.sock \
  --secondary-sock /var/tmp/spdk_secondary.sock \
  --timeout 30 -v
```

**Options**:

| Option | Default | Description |
|--------|---------|-------------|
| `--primary-sock <path>` | `/var/tmp/spdk_hot_upgrade.sock` | Primary RPC socket |
| `--secondary-sock <path>` | `/var/tmp/spdk_secondary.sock` | Secondary RPC socket |
| `--timeout <sec>` | 30 | Per-step timeout |
| `--skip-precheck` | — | Skip pre-check |
| `--skip-commit` | — | Skip commit (don't terminate old Primary) |
| `--dry-run` | — | Print commands without executing |
| `-v` | — | Verbose output |

**Exit codes**: 0=success, 1=artifact/precheck failure, 2=execution failure (auto-rolled back), 3=commit failure, 6=rollback triggered.

### 6.2 Step-by-Step Execution (Advanced)

```bash
# 1. Pre-check (8 items)
scripts/hot_upgrade/spdk_hot_upgrade_precheck.sh \
  -p /var/tmp/spdk_hot_upgrade.sock \
  -n ./new_spdk_tgt \
  -s /var/tmp/spdk_secondary.sock

# 2. Execute hot upgrade (parallel pre-init + IO switch)
scripts/hot_upgrade/spdk_hot_upgrade_do.sh \
  -p /var/tmp/spdk_hot_upgrade.sock \
  -s /var/tmp/spdk_secondary.sock \
  -n ./new_spdk_tgt -t 30 -v

# 3. Commit (terminate old Primary, rename socket)
scripts/hot_upgrade/spdk_hot_upgrade_commit.sh \
  -p <primary_pid> -s /var/tmp/spdk_secondary.sock
```

### 6.3 Rollback

Hot upgrade failure triggers **automatic rollback**. Manual rollback:

```bash
scripts/hot_upgrade/spdk_hot_upgrade_rollback.sh \
  -p <primary_pid> \
  -s /var/tmp/spdk_secondary.sock \
  --secondary-pid <secondary_pid> \
  --old-binary ./old_spdk_tgt \
  --primary-args "-m 0x1 -i 0 --base-virtaddr 0x200000000000"
```

## 7. RPC Interface

| RPC | Caller | Description |
|-----|--------|-------------|
| `primary_exit` | Script → Primary | drain IO → save state → suspend → reactor pause. IO interruption starts |
| `secondary_init` | Script → Secondary | takeover → restore FD/memory/connections → reactor run. IO interruption ends |
| `primary_resume` | Script → Primary | Rollback: resume from SUSPENDED |
| `hot_upgrade_status` | Script | Query current state machine state |
| `hot_upgrade_get_timeline` | Script | Query TSC timeline, measure per-phase IO interruption |
| `secondary_force_reconstruct` | Test script | Force bdev reconstruction bypassing state machine (test only) |

## 8. File Paths

| Path | Purpose |
|------|---------|
| `/var/tmp/spdk_hot_upgrade_state` | Shared state file (Primary writes, Secondary reads) |
| `/var/tmp/spdk_hot_upgrade_timeline` | TSC timeline file |
| `/var/tmp/spdk_hu_ipc.sock` | Primary/Secondary IPC socket (SCM_RIGHTS FD transfer) |
| `/var/tmp/spdk_hot_upgrade.sock` | Primary RPC socket |
| `/var/tmp/spdk_secondary.sock` | Secondary RPC socket |

## 9. Test Results (2026-07-03, Fresh Clone)

Fresh clone from `https://github.com/xzysnd/spdk.git` (branch `master_hot_replace`, commit `a8be7fce9`).

Phase 12 test (`test_tsc_timeline_and_io_interruption.py`) — **3/3 PASSED**:

| Test | TSC IO Interruption | Shell-side IO Interruption | secondary_takeover_done | Result |
|------|:-------------------:|:--------------------------:|:-----------------------:|:------:|
| Test 1 (cold start) | 133ms | 221ms | 119.34ms | PASSED |
| Test 2 (warm start) | 87ms | 162ms | 72.36ms | PASSED |
| Test 3 (warm start) | 57ms | 129ms | 45.41ms | PASSED |

TSC breakdown (Test 3, 57ms total):
```
primary_drain_done:      +0.13ms
primary_suspend_done:    +1.27ms
secondary_init_start:    +10.39ms
secondary_takeover_done: +45.41ms  ← main bottleneck
reactor_running:         +0.15ms
TOTAL:                   57ms
```

Malloc0 bdev reconstructed: `bs=512, nb=131072` — 3/3 tests successful.

> **Cold vs warm**: Test 1 (cold) TSC is higher (133ms) due to cold OS page cache and CPU caches. Test 2/3 (warm) drop to 57–87ms as the binary and libraries are cached in RAM. Production environments typically run warm.

## 10. Limitations & Notes

1. **DPDK version compatibility**: Primary and Secondary must be compatible; precheck rejects cross-major-version upgrades
2. **CPU mask consistency**: Secondary must use the same `--cpumask` as Primary
3. **bdev module support**: Only modules implementing `secondary_reconstruct` (currently malloc) can be rebuilt
4. **vhost session migration**: Current vhost device rebuild uses `ctxt=NULL`; vhost-user sessions (QEMU connections) are not migrated — Phase 13b will address this
5. **Single Secondary**: Currently only one Secondary process is supported
6. **PCI devices**: Secondary does not enumerate PCI devices; NVMe bdevs need additional reconstruction callbacks
7. **libuuid detection**: On some environments (e.g., Lima VM), configure may fail to detect libuuid — manually set `CONFIG_HAVE_LIBUUID=y`
8. **primary_drain_io is stub**: Currently `_next(0)` without actual IO draining or inflight tracking — needs implementation for production

---
---

<!-- ================================================================ -->
<!--                    CHINESE VERSION (SECOND)                     -->
<!-- ================================================================ -->

# 中文版

## 一、功能概述

SPDK 热替换（Hot Upgrade）允许在**不中断 IO 服务**的前提下，将运行中的 SPDK Primary 进程升级到新版本。利用 DPDK 多进程机制：新版本 Secondary 进程在 Primary 仍运行时完成预初始化，然后通过状态机驱动的 drain → suspend → takeover 流程切换 IO 职责，整个过程 **IO 中断目标 < 200ms**。

## 二、核心特性

| 特性 | 说明 |
|------|------|
| 并行预初始化 | Secondary 在 Primary 仍运行时启动并完成子系统预初始化，与 Primary IO 处理重叠 |
| 状态机驱动 | 8 状态原子状态机，编排脚本与 SPDK 内部通过状态轮询协调 |
| bdev 重建 | Primary 保存 bdev 构造参数，Secondary 通过模块回调 `secondary_reconstruct` 重建 bdev |
| vhost FD 传递 | 通过 SCM_RIGHTS 在进程间传递 Guest 内存 FD + rte_vhost 连接状态 |
| TSC 时间线 | 6 个插桩点精确测量 IO 中断各阶段耗时 |
| 自动回滚 | 编排脚本任意步骤失败自动触发回滚，恢复 Primary 运行 |
| 单入口编排 | `spdk_hot_upgrade.sh` 一个脚本完成 precheck → do → commit |

## 三、支持版本与子系统

### 3.1 版本兼容性

- 基于 **SPDK v26.09-pre**（DPDK 26.03.0）
- Primary 与 Secondary 必须使用兼容的 DPDK 版本，precheck 会拒绝跨大版本升级
- 状态文件结构变更后旧文件无法加载（大小校验）

### 3.2 已支持子系统

| 子系统 | drain_io | suspend | secondary_pre_init | secondary_takeover |
|--------|:--------:|:-------:|:------------------:|:------------------:|
| **iobuf** | — | — | ✓（`spdk_iobuf_initialize`） | — |
| **accel** | — | — | ✓（`spdk_accel_initialize`） | — |
| **bdev** | ✓（空操作） | ✓（保存 bdev 参数） | ✓（`spdk_bdev_initialize` + 重建） | stub（重建在 pre_init 完成） |
| **vhost_blk** | ✓ | ✓（提取 FD + 保存设备信息） | ✓（重建 vhost 设备） | ✓（接收 FD + 恢复连接） |
| **vhost_scsi** | ✓ | ✓ | stub | ✓ |

> **拓扑顺序**：iobuf → accel → bdev（通过 `subsystem_sort()` 保证）。这确保 `g_modules_opc` 在 `bdev_register` 解引用前已填充。

### 3.3 支持 bdev 重建的模块

| 模块 | 重建函数 | 说明 |
|------|---------|------|
| malloc | `malloc_secondary_reconstruct` | 用保存的 (name, block_size, num_blocks) 重建 Malloc bdev |

> 其他模块（如 NVMe）需自行实现 `secondary_reconstruct` 回调才能在 Secondary 中重建。

## 四、构建

热替换功能**默认编译**，无需额外 configure 选项。

```bash
./configure
make -j$(nproc)
```

> **注意（Lima VM / 部分 ARM64 环境）**：SPDK 的 configure 脚本可能无法检测到已安装的 libuuid。如果 configure 后 `CONFIG_HAVE_LIBUUID=n`，需手动修改 `CONFIG` 文件：
> ```bash
> sed -i 's/CONFIG_HAVE_LIBUUID=n/CONFIG_HAVE_LIBUUID=y/' CONFIG
> sed -i 's/CONFIG_HAVE_UUID_GENERATE_SHA1=n/CONFIG_HAVE_UUID_GENERATE_SHA1=y/' CONFIG
> make -j$(nproc)
> ```

构建产物：
- `build/bin/spdk_tgt` — SPDK 目标二进制，已链接 hot_upgrade 库
- `build/lib/libspdk_hot_upgrade.so` — 热替换库

## 五、启用热替换

热替换通过**运行时参数**启用，无需构建开关。

### Primary 启动

Primary 必须使用以下参数才能支持后续热替换：

```bash
build/bin/spdk_tgt \
  -m 0x1 \                          # CPU core mask（Secondary 必须继承）
  -i 0 \                            # DPDK shm_id（多进程共享内存标识，必需）
  --base-virtaddr 0x200000000000 \  # DPDK 基址虚拟地址（推荐，保证地址一致）
  -r /var/tmp/spdk_hot_upgrade.sock # RPC socket 路径
```

| 参数 | 必需性 | 说明 |
|------|:------:|------|
| `-i, --shm-id <id>` | **必需** | DPDK 共享内存 ID，Secondary 通过它连接 Primary 的 hugepage |
| `--base-virtaddr <addr>` | 推荐 | DPDK 基址虚拟地址，保证 bdev 指针跨进程有效 |
| `-m, --cpumask <mask>` | 必需 | CPU core mask，Secondary 必须使用相同 mask |
| `-r, --rpc-addr <path>` | 必需 | RPC socket 路径 |

Secondary 由编排脚本自动启动，**无需手动执行**。

## 六、使用方法

### 6.1 单入口编排（推荐）

```bash
# 用法：spdk_hot_upgrade.sh <artifact> [options]
# artifact 支持：tar 包（.tar.gz）/ 目录 / 可执行文件

# 示例：用新版本 tar 包升级
scripts/hot_upgrade/spdk_hot_upgrade.sh ./new_spdk.tar.gz

# 示例：指定 socket + 超时 + 详细输出
scripts/hot_upgrade/spdk_hot_upgrade.sh ./new_spdk.tar.gz \
  --primary-sock /var/tmp/spdk_hot_upgrade.sock \
  --secondary-sock /var/tmp/spdk_secondary.sock \
  --timeout 30 -v
```

**选项**：

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `--primary-sock <path>` | `/var/tmp/spdk_hot_upgrade.sock` | Primary RPC socket |
| `--secondary-sock <path>` | `/var/tmp/spdk_secondary.sock` | Secondary RPC socket |
| `--timeout <sec>` | 30 | 各步骤超时时间 |
| `--skip-precheck` | - | 跳过预检查 |
| `--skip-commit` | - | 跳过提交（不终止旧 Primary） |
| `--dry-run` | - | 只打印命令不执行 |
| `-v` | - | 详细输出 |

**退出码**：0=成功，1=制品/precheck 失败，2=执行失败（已自动回滚），3=commit 失败，6=已触发回滚。

### 6.2 分步执行（高级）

```bash
# 1. 预检查（8 项）
scripts/hot_upgrade/spdk_hot_upgrade_precheck.sh \
  -p /var/tmp/spdk_hot_upgrade.sock \
  -n ./new_spdk_tgt \
  -s /var/tmp/spdk_secondary.sock

# 2. 执行热替换（并行预初始化 + IO 切换）
scripts/hot_upgrade/spdk_hot_upgrade_do.sh \
  -p /var/tmp/spdk_hot_upgrade.sock \
  -s /var/tmp/spdk_secondary.sock \
  -n ./new_spdk_tgt -t 30 -v

# 3. 提交（终止旧 Primary，重命名 socket）
scripts/hot_upgrade/spdk_hot_upgrade_commit.sh \
  -p <primary_pid> -s /var/tmp/spdk_secondary.sock
```

### 6.3 回滚

热替换失败时**自动回滚**。手动回滚：

```bash
scripts/hot_upgrade/spdk_hot_upgrade_rollback.sh \
  -p <primary_pid> \
  -s /var/tmp/spdk_secondary.sock \
  --secondary-pid <secondary_pid> \
  --old-binary ./old_spdk_tgt \
  --primary-args "-m 0x1 -i 0 --base-virtaddr 0x200000000000"
```

## 七、RPC 接口

| RPC | 调用方 | 说明 |
|-----|--------|------|
| `primary_exit` | 编排脚本 → Primary | drain IO → 保存状态 → suspend → reactor 暂停。IO 中断开始 |
| `secondary_init` | 编排脚本 → Secondary | takeover → 恢复 FD/内存/连接 → reactor 运行。IO 中断结束 |
| `primary_resume` | 编排脚本 → Primary | 回滚：从 SUSPENDED 恢复运行 |
| `hot_upgrade_status` | 编排脚本 | 查询当前状态机状态 |
| `hot_upgrade_get_timeline` | 编排脚本 | 查询 TSC 时间线，测量 IO 中断各阶段耗时 |
| `secondary_force_reconstruct` | 测试脚本 | 绕过状态机强制重建 bdev，仅功能测试用 |

## 八、文件路径

| 路径 | 用途 |
|------|------|
| `/var/tmp/spdk_hot_upgrade_state` | 共享状态文件（Primary 写入，Secondary 读取） |
| `/var/tmp/spdk_hot_upgrade_timeline` | TSC 时间线文件 |
| `/var/tmp/spdk_hu_ipc.sock` | Primary/Secondary 间 IPC socket（SCM_RIGHTS FD 传递） |
| `/var/tmp/spdk_hot_upgrade.sock` | Primary RPC socket |
| `/var/tmp/spdk_secondary.sock` | Secondary RPC socket |

## 九、测试结果（2026-07-03，全新克隆）

从 `https://github.com/xzysnd/spdk.git` 全新克隆（分支 `master_hot_replace`，commit `a8be7fce9`）。

Phase 12 测试（`test_tsc_timeline_and_io_interruption.py`）—— **3/3 全部 PASSED**：

| 测试 | TSC IO 中断 | Shell 侧 IO 中断 | secondary_takeover_done | 结果 |
|------|:-----------:|:----------------:|:-----------------------:|:----:|
| Test 1（冷启动） | 133ms | 221ms | 119.34ms | PASSED |
| Test 2（暖启动） | 87ms | 162ms | 72.36ms | PASSED |
| Test 3（暖启动） | 57ms | 129ms | 45.41ms | PASSED |

TSC 各阶段分解（Test 3，总计 57ms）：
```
primary_drain_done:      +0.13ms
primary_suspend_done:    +1.27ms
secondary_init_start:    +10.39ms
secondary_takeover_done: +45.41ms  ← 主要瓶颈
reactor_running:         +0.15ms
TOTAL:                   57ms
```

Malloc0 bdev 重建正确：`bs=512, nb=131072` —— 3/3 测试均成功。

> **冷启动 vs 暖启动**：Test 1（冷）TSC 较高（133ms），因为 OS page cache 和 CPU cache 冷。Test 2/3（暖）降至 57–87ms，因为二进制和库已缓存在 RAM 中。生产环境通常为暖启动。

## 十、限制与注意事项

1. **DPDK 版本兼容**：Primary 和 Secondary 必须兼容，precheck 拒绝跨大版本升级
2. **CPU mask 一致**：Secondary 必须使用与 Primary 相同的 `--cpumask`
3. **bdev 模块支持**：仅实现了 `secondary_reconstruct` 回调的模块（当前为 malloc）才能重建
4. **vhost 会话迁移**：当前 vhost 设备重建用 `ctxt=NULL`，vhost-user 会话（QEMU 连接）不迁移，Phase 13b 完善
5. **单 Secondary**：当前仅支持单个 Secondary 进程
6. **PCI 设备**：Secondary 不枚举 PCI 设备，NVMe bdev 等需额外实现重建回调
7. **libuuid 检测**：部分环境（如 Lima VM）configure 可能无法检测 libuuid — 需手动设置 `CONFIG_HAVE_LIBUUID=y`
8. **primary_drain_io 仍是 stub**：当前直接 `_next(0)`，未实际排空 IO 或追踪 inflight — 生产环境需实现

