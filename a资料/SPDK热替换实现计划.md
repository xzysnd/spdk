# SPDK 进程热替换（Hot Upgrade）实现计划

## Context

当前 SPDK 仓 (`/Users/xuzhiyi/Desktop/work/spdk热替换/spdk`) 完全没有进程热替换功能。本计划基于 **DPDK multi-process + SCM_RIGHTS FD 传递** 机制，为 vhost-user 场景实现 SPDK 进程无中断升级。对 guest VM 透明，IO 中断时间控制在毫秒级。

### 核心技术要点

1. **Primary 挂起而非退出**：Primary 在排空 IO 并传递状态后，通过 `pause()` 进入挂起状态（进程不退出，不销毁 DPDK EAL）。确保大页共享内存元数据不丢失，Secondary 完全接管后 Primary 再安全退出。
2. **SCM_RIGHTS 传递 vhost FD**：Primary 将已建立的 vhost-user socket fd 和 eventfd (kickfd/callfd) 通过 Unix Domain Socket 传递给 Secondary。Secondary 直接将其挂载到自己的 Reactor 中。**QEMU 底层连接不断，完全无感。**
3. **共享数据结构指针继承**（废弃 JSON 回放）：通过强制使用 `spdk_zmalloc(..., SPDK_MALLOC_DMA)` 将 bdev/vhost 关键对象分配在 DPDK 共享大页中。Primary 挂起前将全局管理链表头指针（`g_bdev_mgr`、`g_spdk_vhost_devices`）保存到共享状态文件。Secondary 通过 `--base-virtaddr` 保证虚拟地址相同，直接从共享内存读取继承，无需 JSON 重放。
4. **Guest 内存映射重放**（关键补充）：QEMU 的 Guest 物理内存通过 mmap 映射到进程私有地址空间。Primary 挂起前将此映射关系（fd + 虚拟地址）传递给 Secondary，Secondary 以 `MAP_FIXED` 在相同地址重建映射。
5. **Reactor IDLE/RUNNING 状态控制**：利用 SPDK 现有的 `g_reactor_state` 机制（`reactor.c:1033`），新增 `SPDK_REACTOR_STATE_IDLE` 状态。IDLE 模式下调用 `usleep(1000)` 降低 CPU 占用，同时保证 RPC 通道的 `event_queue_run_batch()` 轮询畅通。

### 对原 EP 双 Socket 方案的改进

原方案采用 EP（Edge Proxy）双 Socket 模式，本方案改为 FD 传递。核心优势：

- **QEMU 完全无感知**：socket 连接从未断开，无需 EP 配合
- **消除位图追踪器复杂度**：Primary 挂起而非退出，大页内存元数据不丢失
- **零 JSON 回放**：直接共享数据结构指针，避免重复分配内存
- **修改范围更精简**：约 25 个文件 → 约 15 个文件

### 最终交付目标（v2 计划增补，2026-06-29）

**用户需求**：通过一个热替换脚本完成 SPDK 进程升级，参数为新版本 SPDK 编译产物（tar 包 / 路径 / 版本号），运行后 SPDK 进程被热替换，几乎不影响业务 IO 中断。

**交付形态**：
```bash
# 单命令完成热替换
./scripts/hot_upgrade/spdk_hot_upgrade.sh <artifact>

# artifact 支持三种形式：
./spdk_hot_upgrade.sh /tmp/spdk-24.01.1.tar.gz        # tar 包路径
./spdk_hot_upgrade.sh /path/to/build/bin/spdk_tgt      # 已解压二进制路径
./spdk_hot_upgrade.sh v24.01.1                         # 版本号（从构建服务器拉取，可选）
```

**核心约束**：
- IO 中断时间 < 200ms（仅 Primary 挂起 → Secondary 接管的临界区）
- Secondary 预初始化与 Primary 正常运行**并行**（不占用 IO 中断窗口）
- 失败自动回退（调用 `primary_resume` RPC，Primary 恢复 IO）
- 全流程可观测（时间戳日志、状态机进度、IO 中断时长度量）

**当前差距与 v2 新增阶段**（在 Phase 0-9 已完成基础上）：

| 差距 | 现状 | 目标 | 对应 Phase |
|------|------|------|-----------|
| bdev 未共享 | ~~Phase 8 是 stub，Secondary 看不到 Primary 的 bdev~~ ✅ 已修复 | Secondary 继承 Primary 全部 bdev | Phase 10 ✅ |
| 脚本串行执行 | ~~`do.sh` 先 `primary_exit` 再启动 Secondary，IO 中断 ~5s~~ ✅ 已修复 | 并行预初始化，IO 中断 <200ms | Phase 11 ✅ |
| 无 artifact 处理 | ~~脚本仅接受二进制路径~~ ✅ 已修复 | 支持 tar/路径/版本号 | Phase 11 ✅ |
| 无单入口 | ~~需手动调 precheck→do→commit~~ ✅ 已修复 | 单命令完成 | Phase 11 ✅ |
| 无 IO 测量 | 无法量化中断时长 | 高精度时间戳 + fio 延迟监控 | Phase 12 |
| 无真实场景测试 | 仅 POC 级 takeover 验证 | QEMU+vhost+fio 端到端 | Phase 13 |

---

## 代码仓库关键验证结果

### ✅ 已验证：Reactor 状态机制已存在（利用现有基础设施）

`lib/event/reactor.c:1008-1036`：

```c
while (1) {
    if (spdk_unlikely(reactor->in_interrupt)) {
        reactor_interrupt_run(reactor);   // epoll_wait 阻塞
    } else {
        _reactor_run(reactor);            // spdk_thread_poll() 忙等待
    }
    if (g_reactor_state != SPDK_REACTOR_STATE_RUNNING) {
        break;                            // 状态机出口已存在！
    }
}
```

`g_reactor_state` 当前仅支持 `UNINITIALIZED → INITIALIZED → RUNNING → EXITING → SHUTDOWN`。只需新增 `SPDK_REACTOR_STATE_IDLE` 状态。

### ⚠️ 已验证：bdev/vhost 对象不在共享大页中（需要修复）

- `lib/vhost/rte_vhost_user.c:924`：`posix_memalign()` — **进程私有堆**
- `lib/vhost/vhost.c:146`：`strdup()` — **标准 malloc**
- `lib/vhost/vhost.c:27`：`g_vhost_devices` — **`.bss` 段（进程私有）**

Secondary 进程看不到这些对象。JSON 回放会重新创建冲突的副本。

### ⚠️ 已验证：Guest 内存 mmap 进程私有（需要修复）

`lib/vhost/rte_vhost_user.c:733-745` 调用 `spdk_mem_register()`（底层 `mmap`），映射是进程私有的。Secondary 必须重建映射。

---

## 热替换完整流程图

```
Primary 正常运行，处理 IO
  │
  ├── 阶段一：预初始化（Primary 仍在运行，IO 不中断）
  │     ├── Step 1. 发起升级
  │     │       编排器调用 SPDK 热替换升级脚本
  │     │       安装新版本 SPDK 二进制包到指定目录
  │     ├── Step 2. Secondary 预初始化
  │     │       2a. DPDK 环境初始化（--proc-type=secondary, 相同 shm_id, base_virtaddr, core_mask）
  │     │       2b. 连接 Primary 的 IPC socket
  │     │       2c. 映射共享 memzone/mempool（直接 lookup，不做 reset）
  │     │       2d. 从共享状态文件读取全局管理链表头指针
  │     │       2e. 共享内存中直接访问 bdev/vhost 结构（--base_virtaddr 相同，指针有效）
  │     │       2f. 初始化 Reactor（状态=IDLE，usleep 低CPU模式，仍可处理RPC）
  │     │       2g. 启动 RPC server（注册 secondary_init RPC）
  │     │       2h. 阻塞等待编排器指令
  │     │       耗时：~3-5秒（DPDK init，IO 不中断）
  │
  ├── 阶段二：切换接管（IO 中断开始 ~ 毫秒级）
  │     ├── Step 3. Primary 排空并挂起
  │     │       3a. 编排器发送 RPC: primary_exit
  │     │       3b. Primary 停止接收新 IO，排空在途 IO（超时保护 5秒）
  │     │       3c. 提取全局管理链表头指针 → 写入共享状态文件
  │     │       3d. 提取 Guest 内存映射元数据（fd + virtaddr → 写入共享状态文件）
  │     │       3e. 提取 DPDK rte_vhost 内部状态（vid 上下文、features、mem_table）
  │     │       3f. 停止 vhost session poller（不关闭 socket/eventfd）
  │     │       3g. 从 DPDK fdset epoll 中删除 kickfd 的监听
  │     │       3h. 提取 sock_fd + kick_fds + mem_fds，通过 IPC socket 发送（SCM_RIGHTS）
  │     │       3i. Primary 进入 pause() 挂起
  │     │       3j. 回复 RPC 成功
  │     ├── Step 4. Secondary 接管
  │     │       4a. 编排器发送 RPC: secondary_init
  │     │       4b. Secondary 接收 FD，从共享状态文件读指针+映射表
  │     │       4c. mmap Guest 内存 fd 到相同虚拟地址（MAP_FIXED）
  │     │       4d. 在 DPDK vhost_devices 数组中恢复 vid 上下文
  │     │       4e. 将 sock_fd/kick_fd 注入 Reactor epoll
  │     │       4f. 注册 vhost process_vq poller
  │     │       4g. 重建 I/O channels（spdk_io_channel）
  │     │       4h. 唤醒 Reactor（状态改为 RUNNING）
  │     │       4i. IO 恢复（QEMU 无感知）
  │
  └── 阶段三：清理退出
        ├── Step 5. 升级提交
        │       5a. 编排器确认 Secondary 正常运行
        │       5b. 向 Primary 发送 SIGTERM（安全退出）
        │       5c. 清理状态文件和 IPC socket
        │       5d. 卸载旧版本 SPDK 二进制包
        └── Step 6. 升级回退（异常处理）
                6a. Secondary 预初始化失败 → Primary 继续运行
                6b. FD 注入/接管失败 → rpc_primary_resume 恢复 Primary
                6c. 超时保护 → 强制回退
```

---

## 分层实现详细设计

### Phase 0: 内存与环境基础设施

**核心目标**：保证 Secondary 能无缝复用 Primary 的内存状态。

#### 0.1 启动参数强约束

主备进程启动时必须指定相同的参数：
```
--base-virtaddr=0x200000000000  # 大页内存起始虚拟地址
--shm-id=<N>                    # DPDK multi-process shm ID
-m <core_mask>                  # CPU core mask（必须相同！）
```

- 必须使用相同 core_mask：因为 Secondary 接管后必须在相同物理核上运行 reactor，才能保持 L1/L2 cache 热度。切换核会导致严重 cache miss。
- Primary 启动时保存 `base_virtaddr` + `core_mask` 到共享状态文件
- Secondary 启动时从文件读取，使用相同值调用 `rte_eal_init()`

#### 0.2 强制共享分配 — bdev/vhost 关键对象（修复陷阱1）

修改以下分配点，将关键结构体迁移到共享大页中：

| 原分配方式 | 位置 | 修改为 |
|-----------|------|--------|
| `posix_memalign()` | `lib/vhost/rte_vhost_user.c:924` | `spdk_zmalloc(..., SPDK_MALLOC_DMA)` |
| `strdup()` | `lib/vhost/vhost.c:146` | `spdk_dma_malloc()` |
| `calloc()` | `lib/bdev/bdev.c` | `spdk_zmalloc(..., SPDK_MALLOC_DMA)` |

```c
// 示例：修改 vhost session 分配
// 原：posix_memalign((void **)&vsession, SPDK_CACHE_LINE_SIZE, ...);
// 改：
vsession = spdk_zmalloc(sizeof(*vsession) + user_backend->session_ctx_size,
                         SPDK_CACHE_LINE_SIZE, NULL,
                         SPDK_ENV_SOCKET_ID_ANY, SPDK_MALLOC_DMA);
```

#### 0.3 全局管理链表头指针传递（替代 JSON 回放）

Primary 挂起前，将全局管理结构的头指针保存：

```c
struct spdk_hot_upgrade_shared_state {
    ...
    /* bdev 子系统全局指针 */
    uint64_t bdev_mgr_addr;              // &g_bdev_mgr 的虚拟地址
    uint64_t bdev_mgr_bdevs_head;        // g_bdev_mgr.bdevs TAILQ 头节点地址

    /* vhost 子系统全局指针 */
    uint64_t vhost_devices_root;         // g_vhost_devices RB_ROOT 地址

    /* vhost session 列表 */
    uint32_t num_vhost_sessions;
    uint64_t vhost_session_addrs[64];    // 各 session 的虚拟地址
};
```

**Secondary 直接继承**（不解析 JSON）：
```c
int spdk_hot_upgrade_load_shared_objects(void)
{
    struct spdk_hot_upgrade_shared_state *state = spdk_hot_upgrade_state_load();

    // 直接映射共享对象的虚拟地址（--base_virtaddr 相同，指针有效）
    g_bdev_mgr = *(struct spdk_bdev_mgr *)state->bdev_mgr_addr;
    // g_bdev_mgr.bdevs 链表的所有节点都在共享大页中，指针全部有效

    g_vhost_devices = *(struct vhost_dev_name_tree *)state->vhost_devices_root;
    // 同理，所有 vhost_dev 也都在共享大页中

    return 0;
}
```

**JSON 配置仅作为离线备份**，不参与自动化热升级流程。

#### 0.4 三类大页内存的直接复用

| 类型 | 分配方式 | Primary 退出处理 | Secondary 处理 |
|------|---------|-----------------|---------------|
| **memzone** | `spdk_memzone_reserve()` | **不释放** | `spdk_memzone_lookup()` 获取 |
| **mempool** | `spdk_mempool_create()` | **不释放** | `spdk_mempool_lookup()` 获取，直接作为消费者 |
| **ring** | `spdk_ring_create()` | **不释放** | 共享状态文件记录 PID 映射表，Secondary lookup 时动态拼装名称 |
| **spdk_malloc** | `spdk_malloc()` / `spdk_dma_malloc()` | **不释放** | DPDK heap 在共享大页中，直接继承 |
| **spdk_zmalloc(DMA)** | bdev/vhost 对象 | **不释放** | Secondary 通过共享指针直接访问 |

关键变化：
- ❌ **彻底废弃** JSON 回放机制（避免内存重复分配和链表冲突）
- ❌ **废弃**位图内存追踪器（Primary 挂起不释放）
- ✅ 强制 bdev/vhost 对象分配在共享内存中
- ✅ 通过全局头指针直接继承所有活跃对象

---

### Phase 1: 框架层 — 热升级状态机

#### 1.1 新增文件：`include/spdk/hot_upgrade.h`

```c
enum spdk_hot_upgrade_state {
    SPDK_HU_IDLE = 0,
    SPDK_HU_SECONDARY_PRE_INIT,
    SPDK_HU_SECONDARY_PRE_INIT_DONE,
    SPDK_HU_PRIMARY_DRAINING,
    SPDK_HU_PRIMARY_SUSPENDED,
    SPDK_HU_SECONDARY_TAKEOVER,
    SPDK_HU_COMPLETE,
    SPDK_HU_FAILED,
};

void spdk_hot_upgrade_set_state(enum spdk_hot_upgrade_state state);
enum spdk_hot_upgrade_state spdk_hot_upgrade_get_state(void);
```

#### 1.2 新增文件：`lib/hot_upgrade/hot_upgrade.c`

- 全局状态管理 + 状态转换合法性校验
- 内部 IPC socket 管理（`/var/tmp/spdk_hu_ipc.sock`）
- 热替换失败时的回退逻辑

#### 1.3 新增：`lib/hot_upgrade/Makefile`

#### 1.4 修改：`lib/Makefile` — 添加 `hot_upgrade` 子目录

---

### Phase 2: 框架层 — Subsystem 接口扩展

#### 2.1 修改：`include/spdk/init.h`

```c
struct spdk_subsystem {
    const char *name;
    void (*init)(void);
    void (*fini)(void);
    void (*write_config_json)(struct spdk_json_write_ctx *w);

    /* 热替换回调 — 新增 */
    void (*primary_drain_io)(void);
    void (*primary_suspend)(void);
    void (*secondary_pre_init)(void);
    void (*secondary_takeover)(void);

    TAILQ_ENTRY(spdk_subsystem) tailq;
};
```

新增框架层 API：

```c
void spdk_subsystem_primary_drain_io(spdk_subsystem_fini_fn cb_fn, void *cb_arg);
void spdk_subsystem_primary_drain_io_next(int rc);
void spdk_subsystem_primary_suspend(spdk_subsystem_fini_fn cb_fn, void *cb_arg);
void spdk_subsystem_primary_suspend_next(int rc);
void spdk_subsystem_secondary_pre_init(spdk_subsystem_init_fn cb_fn, void *cb_arg);
void spdk_subsystem_secondary_pre_init_next(int rc);
void spdk_subsystem_secondary_takeover(spdk_subsystem_init_fn cb_fn, void *cb_arg);
void spdk_subsystem_secondary_takeover_next(int rc);
```

#### 2.2 修改：`lib/init/subsystem.c` / `lib/init/subsystem.h`

- 实现四个遍历函数，对 `g_subsystems` 链表排序后依次调用对应回调
- 新回调为 NULL 时跳过，直接调用 `_next()` 推进

---

### Phase 3: App 框架与 Secondary 启动流程

#### 3.1 修改：`include/spdk/event.h`

```c
int spdk_app_secondary_pre_init(struct spdk_app_opts *opts);
int spdk_app_secondary_full_init(struct spdk_app_opts *opts,
                                  spdk_msg_fn start_fn, void *start_arg);
```

#### 3.2 修改：`lib/event/app.c`

**Secondary 预初始化**（`spdk_app_secondary_pre_init`）：

```c
int spdk_app_secondary_pre_init(struct spdk_app_opts *opts)
{
    // 1. 设置状态 SPDK_HU_SECONDARY_PRE_INIT
    // 2. spdk_env_init(--proc-type=secondary, opts)
    // 3. 连接 Primary 的 IPC socket
    // 4. 映射共享 memzone/mempool（lookup）
    // 5. 读取共享状态文件（全局头指针等）
    // 6. 调用 spdk_subsystem_secondary_pre_init()
    //    - 各子系统在共享内存中直接读取对象，不分配新内存
    // 7. 初始化 Reactor（状态=IDLE，usleep 模式，仍处理RPC）
    // 8. 启动 RPC server，注册 secondary_init 方法
}
```

**Secondary 完全接管**（`spdk_app_secondary_full_init`）：

```c
int spdk_app_secondary_full_init(struct spdk_app_opts *opts,
                                  spdk_msg_fn start_fn, void *start_arg)
{
    // 1. 设置状态 SPDK_HU_SECONDARY_TAKEOVER
    // 2. 接收 FD（recvmsg + SCM_RIGHTS）
    // 3. 接收 Guest 内存映射元数据，mmap 重建映射（修复陷阱2）
    // 4. 恢复 DPDK rte_vhost 内部 vid 上下文
    // 5. 调用 spdk_subsystem_secondary_takeover()
    //    - vhost: 注入 FD + 注册 poller
    //    - bdev: 重建 spdk_io_channel
    // 6. 设置 g_reactor_state = SPDK_REACTOR_STATE_RUNNING
    // 7. 设置状态 SPDK_HU_COMPLETE
}
```

#### 3.3 Primary 退出流程

```c
static void app_primary_exit(void)
{
    // 1. 设置状态 SPDK_HU_PRIMARY_DRAINING
    // 2. spdk_subsystem_primary_drain_io() — 排空 IO（超时5秒）
    // 3. 提取全局管理链表头指针 → 写入共享状态文件
    // 4. spdk_subsystem_primary_suspend() — 各子系统：
    //    a. 保存 Guest 内存映射（fd + virtaddr）
    //    b. 提取 DPDK rte_vhost 内部状态
    //    c. 停止 poller，从 epoll 删除 kickfd 监听（修复陷阱4）
    //    d. 提取 sock_fd + kick_fds，SCM_RIGHTS 发送
    // 5. 设置状态 SPDK_HU_PRIMARY_SUSPENDED
    // 6. pause() — 进入挂起
}
```

#### 3.4 Reactor IDLE 状态改造（修复陷阱3）

修改 `lib/event/reactor.c`：

```c
enum spdk_reactor_state {
    SPDK_REACTOR_STATE_UNINITIALIZED = 0,
    SPDK_REACTOR_STATE_INITIALIZED,
    SPDK_REACTOR_STATE_IDLE,       // 新增：低 CPU 模式
    SPDK_REACTOR_STATE_RUNNING,
    SPDK_REACTOR_STATE_EXITING,
    SPDK_REACTOR_STATE_SHUTDOWN,
};
static enum spdk_reactor_state g_reactor_state;

// reactor_run() 修改：
static int reactor_run(void *arg)
{
    ...
    while (1) {
        if (g_reactor_state == SPDK_REACTOR_STATE_IDLE) {
            // 低速轮询：仍调用 event_queue_run_batch 处理 RPC 消息
            event_queue_run_batch(reactor);
            // 主动让出 CPU，避免 100% 占用
            usleep(1000);
            continue;  // 不调用 spdk_thread_poll（业务线程不在）
        }
        // poll 模式正常逻辑
        if (spdk_unlikely(reactor->in_interrupt)) {
            reactor_interrupt_run(reactor);
        } else {
            _reactor_run(reactor);
        }
        ...
        if (g_reactor_state != SPDK_REACTOR_STATE_RUNNING &&
            g_reactor_state != SPDK_REACTOR_STATE_IDLE) {
            break;
        }
    }
}
```

**关键设计**：IDLE 状态下：
- ✅ 仍轮询 `event_queue_run_batch()` → RPC 消息通道畅通
- ✅ `usleep(1000)` → 降低 CPU 占用，不与 Primary 争抢
- ❌ 不调用 `spdk_thread_poll()` → 业务线程暂停

---

### Phase 4: Vhost-User FD 传递与无缝接管（核心）

这是实现 QEMU 无感的最关键步骤。需要深度侵入 DPDK `rte_vhost` 库。

#### 4.1 Guest 内存 mmap 重放（修复陷阱2）

**问题**：`vhost_session_mem_register()` 调用 `spdk_mem_register()` → `mmap()`，映射是进程私有的。Secondary 进程中不存在 Guest 内存的虚拟地址映射。

**Primary 侧**：提取内存映射元数据

```c
struct spdk_hu_mem_region {
    int fd;                  // Guest 内存的文件描述符
    uint64_t mmap_addr;      // Primary 中的映射虚拟地址
    uint64_t mmap_size;      // 映射大小
    uint64_t guest_phys_addr;// Guest 物理地址
};
```

```c
void spdk_vhost_send_mem_fds(struct spdk_vhost_dev *vdev)
{
    struct spdk_vhost_session *vsession;
    TAILQ_FOREACH(vsession, &to_user_dev(vdev)->vsessions, tailq) {
        struct rte_vhost_memory *mem = vsession->mem;
        for (uint32_t i = 0; i < mem->nregions; i++) {
            struct spdk_hu_mem_region region = {
                .fd = mem->regions[i].fd,
                .mmap_addr = mem->regions[i].mmap_addr,
                .mmap_size = mem->regions[i].mmap_size,
                .guest_phys_addr = mem->regions[i].guest_phys_addr,
            };
            // 1. 将 fd 通过 SCM_RIGHTS 发送给 Secondary
            send_fd(ipc_sock, region.fd);
            // 2. 将元数据写入共享状态
            save_mem_region_metadata(&region);
        }
    }
}
```

**Secondary 侧**：重建内存映射

```c
int spdk_vhost_restore_mem_mapping(struct spdk_vhost_session *vsession)
{
    struct spdk_hu_mem_region *regions = get_shared_mem_regions();
    int num_regions = get_shared_num_regions();

    for (int i = 0; i < num_regions; i++) {
        // 关键：使用 MAP_FIXED 在相同虚拟地址重建映射
        void *addr = mmap((void *)regions[i].mmap_addr,
                          regions[i].mmap_size,
                          PROT_READ | PROT_WRITE,
                          MAP_FIXED | MAP_SHARED,  // MAP_FIXED 要特别小心
                          regions[i].fd, 0);
        if (addr == MAP_FAILED) {
            SPDK_ERRLOG("Failed to restore mem mapping at %p\n",
                        (void *)regions[i].mmap_addr);
            return -1;
        }
        assert(addr == (void *)regions[i].mmap_addr);
    }
    return 0;
}
```

**`MAP_FIXED` 安全性保证**：
- 因为 Primary 和 Secondary 使用相同的 `--base-virtaddr`
- DPDK 的大页内存主备都映射在相同的虚拟地址区域
- Guest 内存的 mmap 区域通常在大页范围之后的安全地址空间
- 这些地址在 Secondary 进程中未被占用

#### 4.2 Primary epoll 清理（修复陷阱4）

**问题**：Primary 的 DPDK `rte_vhost` 内部 `fdset` epoll 中仍注册了 `kickfd`。Guest kick 会唤醒 `pause()` 状态下的 Primary。

**修正**：

```c
void spdk_vhost_cleanup_epoll_before_pause(struct spdk_vhost_dev *vdev)
{
    struct spdk_vhost_session *vsession;
    TAILQ_FOREACH(vsession, &to_user_dev(vdev)->vsessions, tailq) {
        for (uint16_t i = 0; i < vsession->max_queues; i++) {
            int kickfd = vsession->virtqueue[i].vring.kickfd;
            // 从 DPDK fdset epoll 中删除 kickfd 监听
            rte_vhost_del_fd_from_fdset(vsession->vid, kickfd);
        }
    }
    // 之后才能安全调用 pause()
}
```

#### 4.3 DPDK rte_vhost 内部状态恢复（关键侵入修改）

**问题**：DPDK `rte_vhost` 内部维护 `vid → vhost_user_connection` 的状态机。如果 Guest 发来 vhost-user 控制消息（如 SET_CONFIG），Secondary 的 `rte_vhost` 必须有合法的 vid 上下文。

**Primary 侧**：提取并传递内部状态

```c
// Primary 连 vhost_user_connection 的内部字段也通过共享内存传递
struct spdk_hu_vhost_conn_state {
    int vid;
    uint64_t protocol_features;
    uint64_t negotiated_features;
    uint64_t virtio_features;
    // ... 其他关键字段
};
```

**Secondary 侧**：注入 DPDK 内部数组

```c
int spdk_vhost_restore_connection(struct spdk_vhost_session *vsession,
                                    int sock_fd, struct spdk_hu_vhost_conn_state *state)
{
    // 1. 直接/通过 DPDK 扩展 API 在 vhost_devices[vid] 中注册
    //    需要 DPDK 补丁：rte_vhost_attach_secondary_session()
    int rc = rte_vhost_attach_session_from_primary(
        state->vid,
        sock_fd,
        state->protocol_features,
        state->negotiated_features,
        ...);
    if (rc != 0) {
        return rc;
    }

    // 2. 验证 vid 上下文可正常使用
    assert(rte_vhost_get_vhost_vring(state->vid, ...) == 0);
    assert(rte_vhost_va_from_guest_pa(...) != 0);

    return 0;
}
```

#### 4.4 Primary 挂起与 FD 发送

Primary 执行 `primary_suspend` 时：

```c
void vhost_blk_primary_suspend(void)
{
    struct spdk_vhost_dev *vdev;
    for (vdev = spdk_vhost_dev_next(NULL); vdev != NULL;
         vdev = spdk_vhost_dev_next(vdev)) {
        // 1. 停止 session poller
        vhost_user_dev_stop_poller(vdev);

        // 2. 保存 Guest 内存映射元数据 + 发送 mem_fds
        spdk_vhost_send_mem_fds(vdev);

        // 3. 提取 DPDK rte_vhost 内部状态
        spdk_vhost_extract_conn_state(vdev);

        // 4. 从 DPDK fdset epoll 删除 kickfd 监听
        spdk_vhost_cleanup_epoll_before_pause(vdev);

        // 5. 提取 sock_fd + kick_fds，SCM_RIGHTS 发送
        spdk_vhost_send_fds_to_secondary(vdev);
    }
    spdk_subsystem_primary_suspend_next(0);
}
```

#### 4.5 Secondary 接收与注入

Secondary 执行 `secondary_takeover` 时：

```c
void vhost_blk_secondary_takeover(void)
{
    struct spdk_vhost_dev *vdev;
    for (vdev = spdk_vhost_dev_next(NULL); vdev != NULL;
         vdev = spdk_vhost_dev_next(vdev)) {
        // 1. 接收 FD
        spdk_vhost_recv_fds_from_primary(vdev);

        // 2. 接收 mem_fds + 重建 Guest 内存映射
        spdk_vhost_restore_mem_mapping_for_dev(vdev);

        // 3. 恢复 DPDK rte_vhost vid 上下文
        spdk_vhost_restore_connections(vdev);

        // 4. 将 sock_fd/kick_fd 注入 Reactor epoll
        spdk_vhost_session_attach_fds(vsession, ...);

        // 5. 注册 process_vq poller
        spdk_vhost_register_poller(vsession);
    }
    spdk_subsystem_secondary_takeover_next(0);
}
```

---

### Phase 5: 共享状态文件（精简版）

#### 5.1 新增文件：`include/spdk/hot_upgrade_shared.h`

```c
#define SPDK_HU_IPC_SOCK_PATH     "/var/tmp/spdk_hu_ipc.sock"
#define SPDK_HU_STATE_FILE        "/var/tmp/spdk_hot_upgrade_state"

struct spdk_hot_upgrade_shared_state {
    uint32_t magic;                    // 0x5350444B ("SPDK")
    uint32_t version;
    uint64_t base_virtaddr;
    int shm_id;
    struct spdk_cpuset core_mask;      // CPU 核掩码
    char ipc_sock_path[108];

    /* DPDK 环境信息 */
    char memzone_names[32][64];
    char mempool_names[32][64];
    uint32_t num_memzones;
    uint32_t num_mempools;

    /* Ring 名称 PID 映射 */
    uint32_t primary_pid;
    char ring_name_template[32][64];

    /* 全局管理链表头指针（v2 核心新增） */
    uint64_t bdev_mgr_addr;
    uint64_t bdev_mgr_bdevs_head;
    uint64_t vhost_devices_root;
    uint32_t num_vhost_sessions;
    uint64_t vhost_session_addrs[64];

    /* Guest 内存映射元数据 */
    uint32_t num_mem_regions;
    struct {
        uint64_t mmap_addr;
        uint64_t mmap_size;
        uint64_t guest_phys_addr;
    } mem_regions[16];

    /* DPDK rte_vhost 内部连接状态 */
    uint32_t num_vhost_conns;
    struct {
        int vid;
        uint64_t protocol_features;
        uint64_t negotiated_features;
    } vhost_conns[16];

    /* 通用应用状态 */
    char rpc_addr[108];
};
```

#### 5.2 状态文件格式

```
/var/tmp/spdk_hot_upgrade_state  — [mmap] 全套共享状态（magic, 头指针, 内存映射表, conn状态等）
/var/tmp/spdk_hu_ipc.sock        — [Unix Socket] Primary↔Secondary IPC 通道（FD + 数据传输）
```

JSON 配置文件仅作为运维备份，不用于自动化热升级：

```
/var/tmp/spdk_hot_upgrade_config.json — [file] 离线备份，热升级路径不使用
```

#### 5.3 `lib/hot_upgrade/hot_upgrade.c` API

```c
int spdk_hot_upgrade_state_save(void);
int spdk_hot_upgrade_state_load(struct spdk_hot_upgrade_shared_state **state);
void spdk_hot_upgrade_state_file_cleanup(void);
int spdk_hot_upgrade_get_ipc_sock(void);
int spdk_hot_upgrade_create_ipc_sock(void);
int spdk_hot_upgrade_connect_ipc_sock(void);
```

---

### Phase 6: RPC 端点

#### 6.1 新增：`lib/hot_upgrade/hot_upgrade_rpc.c`

```c
SPDK_RPC_REGISTER("primary_exit", rpc_primary_exit, SPDK_RPC_RUNTIME)
SPDK_RPC_REGISTER("secondary_init", rpc_secondary_init, SPDK_RPC_RUNTIME)
SPDK_RPC_REGISTER("hot_upgrade_status", rpc_hot_upgrade_status, SPDK_RPC_RUNTIME)
SPDK_RPC_REGISTER("primary_resume", rpc_primary_resume, SPDK_RPC_RUNTIME)  // 回退
```

#### 6.2 RPC 方法说明

**`rpc_primary_exit`**：
1. 设置状态 `SPDK_HU_PRIMARY_DRAINING`
2. 排空 IO → 超时 5 秒
3. 提取全局头指针 + 内存映射 + conn 状态 → 写入共享状态文件
4. 调用 `spdk_subsystem_primary_suspend()`
5. 设置状态 `SPDK_HU_PRIMARY_SUSPENDED`
6. `pause()` 挂起

**`rpc_secondary_init`**：
1. 设置状态 `SPDK_HU_SECONDARY_TAKEOVER`
2. 接收 FD + mmap Guest 内存 + 恢复 conn 状态
3. 调用 `spdk_subsystem_secondary_takeover()`
4. 设置 `g_reactor_state = RUNNING`
5. 设置状态 `SPDK_HU_COMPLETE`

**`rpc_hot_upgrade_status`**：返回当前状态

**`rpc_primary_resume`**（回退）：
仅在 `SPDK_HU_PRIMARY_SUSPENDED` 状态可用。Primary 重新注册 poller，恢复 IO。

---

### Phase 7: 子系统实现 — vhost 热替换（核心）

#### 7.1 修改：`lib/vhost/vhost_internal.h`

```c
struct spdk_vhost_dev {
    ...
    bool is_hu_suspended;
    int  ipc_sock_for_hu;
};
```

#### 7.2 修改：`module/event/subsystems/vhost_blk/vhost_blk.c`

```c
static struct spdk_subsystem g_spdk_subsystem_vhost_blk = {
    .name = "vhost_blk",
    .init = vhost_blk_subsystem_init,
    .fini = vhost_blk_subsystem_fini,
    .write_config_json = spdk_vhost_blk_config_json,

    .primary_drain_io = vhost_blk_primary_drain_io,
    .primary_suspend = vhost_blk_primary_suspend,       // 提取 FD + Guest 内存映射传递
    .secondary_pre_init = vhost_blk_secondary_pre_init,  // 共享内存继承指针
    .secondary_takeover = vhost_blk_secondary_takeover,   // FD 注入 + mmap 重放
};
```

#### 7.3 修改：`module/event/subsystems/vhost_scsi/vhost_scsi.c` — 同 vhost_blk

#### 7.4 新增：`lib/vhost/vhost_hu.c`

```c
// Primary 端
int spdk_vhost_extract_mem_fds(struct spdk_vhost_dev *vdev);
int spdk_vhost_extract_conn_state(struct spdk_vhost_dev *vdev);
void spdk_vhost_cleanup_epoll_before_pause(struct spdk_vhost_dev *vdev);
int spdk_vhost_send_fds_to_secondary(struct spdk_vhost_dev *vdev);

// Secondary 端
int spdk_vhost_recv_fds_from_primary(struct spdk_vhost_dev *vdev);
int spdk_vhost_restore_mem_mapping(struct spdk_vhost_session *vsession);
int spdk_vhost_restore_connections(struct spdk_vhost_dev *vdev);
int spdk_vhost_session_attach_fds(struct spdk_vhost_session *vsession,
                                    int sock_fd, int *kick_fds, uint16_t num_queues);
```

---

### Phase 8: Bdev 子系统适配

#### 8.1 修改：`module/event/subsystems/bdev/bdev.c`

```c
static struct spdk_subsystem g_spdk_subsystem_bdev = {
    .name = "bdev",
    ...
    .primary_drain_io = bdev_subsystem_primary_drain_io,
    .primary_suspend = bdev_subsystem_primary_suspend,
    .secondary_pre_init = bdev_subsystem_secondary_pre_init,  // 共享指针继承
    .secondary_takeover = bdev_subsystem_secondary_takeover,   // rebuild io_channel
};
```

#### 8.2 修改：`module/bdev/bdev.c`

- 修改 `spdk_bdev` 分配：`spdk_zmalloc(..., SPDK_MALLOC_DMA)`
- `secondary_takeover` 中在新 Reactor 的 spdk_thread 上重建 `spdk_io_channel`

---

### Phase 9: 外部编排脚本

#### 9.1 升级前检查：`scripts/hot_upgrade/spdk_hot_upgrade_precheck.sh`

#### 9.2 升级执行：`scripts/hot_upgrade/spdk_hot_upgrade_do.sh`

```bash
# Step 1: 启动 Secondary 预初始化
./new_spdk/build/bin/spdk_tgt --shm-id=<N> --base-virtaddr=<ADDR> -m <MASK> --proc-type=secondary &
wait_for_secondary_ready()

# Step 2: 触发 Primary 退出+挂起
./scripts/rpc.py primary_exit
wait_for_primary_suspended()

# Step 3: 通知 Secondary 接管
./scripts/rpc.py -s <secondary_rpc_sock> secondary_init
wait_for_secondary_active()
```

#### 9.3 升级提交：`scripts/hot_upgrade/spdk_hot_upgrade_commit.sh`

#### 9.4 升级回退：`scripts/hot_upgrade/spdk_hot_upgrade_rollback.sh`

---

### Phase 10: bdev 共享实现（✅ 完成）

**背景**：Phase 8 的 `bdev_secondary_takeover` 是 stub（仅调用 `_next(0)`）。Secondary 启动后看不到 Primary 创建的 bdev（`bdev_get_bdevs` 返回空），takeover 不完整。根因：`g_bdev_mgr.bdevs` TAILQ 头是进程私有 `.bss` 全局变量，PIE 二进制中 `fn_table`/`module` 等指针在 Primary/Secondary 虚拟地址不同。

**目标**：Secondary 接管后，`bdev_get_bdevs` 能返回 Primary 创建的全部 bdev，且可对其发起 IO。

**实际实现方案**（与原计划不同：不改 `g_bdev_mgr` 为指针，而是转移 TAILQ 头值 + fixup PIE 指针）：

#### 10.1 TAILQ 头值传递（而非 g_bdev_mgr 指针化）

`spdk_bdev` 对象本身已通过 `spdk_zalloc(SPDK_MALLOC_DMA)` 分配在 DPDK 共享大页，`--base-virtaddr` + `MAP_FIXED` 保证两进程地址一致。只需传递 `g_bdev_mgr.bdevs` TAILQ 的 `tqh_first`/`tqh_last` 值：

Primary `bdev_primary_suspend` 保存到状态文件：
```c
local_state.bdevs_first = spdk_bdev_hu_get_bdevs_first();  // TAILQ_FIRST
local_state.bdevs_last  = spdk_bdev_hu_get_bdevs_last();   // tqh_last
spdk_bdev_hu_save_bdev_infos(local_state.bdev_infos, ...); // 每个 bdev 的地址+模块名
```

Secondary `bdev_secondary_takeover` 恢复：
```c
spdk_bdev_hu_set_bdevs(state->bdevs_first, state->bdevs_last);
spdk_bdev_hu_fixup_inherited_bdevs(state->bdev_infos, state->num_bdev_infos);
```

#### 10.2 PIE 指针 Fixup（核心修复）

PIE 二进制中 `bdev->fn_table` 和 `bdev->module` 指向各自进程的 `static const` 全局变量，地址不同。Secondary 必须修复这些指针：

1. **`struct spdk_bdev_module` 新增 `fn_table` 字段**（`include/spdk/bdev_module.h`），各模块在定义时填充 `.fn_table = &xxx_fn_table`
2. **`spdk_bdev_hu_fixup_inherited_bdevs`** 按 `bdev_addr` 匹配，按 `module_name` 查找 Secondary 自身的 module，然后：
   - `bdev->module = module`（指向 Secondary 的 module 结构）
   - `bdev->fn_table = module->fn_table`（指向 Secondary 的 fn_table）
   - `TAILQ_INIT(&bdev->aliases)`（alias 对象在 Primary 私有堆，清空）
   - `bdev->internal.qos = NULL`（qos 对象在 Primary 私有堆，清空）

#### 10.3 bdev 名称/产品名共享

`bdev->name` 和 `bdev->product_name` 通过 `malloc_strdup_dma`（`spdk_zalloc(SPDK_MALLOC_DMA)`）分配在共享大页，Secondary 可直接读取。

#### 10.4 验证测试（✅ 通过）

`scripts/hot_upgrade/test_phase10.py` 验证：
1. Primary 创建 Malloc0 ✅
2. `primary_exit` → Primary SUSPENDED ✅
3. 状态文件包含 4 个 DPDK 范围指针（bdevs_first/last + bdev_addr）✅
4. Secondary pre_init → SECONDARY_PRE_INIT_DONE ✅
5. `secondary_init` → COMPLETE ✅
6. Secondary `bdev_get_bdevs` → 返回 Malloc0 ✅

#### 10.5 涉及文件

| 文件 | 变更 |
|------|------|
| `lib/bdev/bdev.c` | 新增 `spdk_bdev_hu_get_bdevs_first/last`、`spdk_bdev_hu_set_bdevs`、`spdk_bdev_hu_save_bdev_infos`、`spdk_bdev_hu_fixup_inherited_bdevs`；`dump_info_json` 加 `fn_table` NULL 检查 |
| `module/event/subsystems/bdev/bdev.c` | `bdev_primary_suspend` 保存 TAILQ 头+bdev_infos；`bdev_secondary_takeover` 恢复+fixup |
| `include/spdk/bdev_module.h` | `struct spdk_bdev_module` 新增 `fn_table` 字段 |
| `module/bdev/malloc/bdev_malloc.c` | `malloc_if` 填充 `.fn_table = &malloc_fn_table`；`malloc_strdup_dma` 共享名称分配 |
| `include/spdk/hot_upgrade_shared.h` | `bdevs_first`/`bdevs_last`/`bdev_infos`/`num_bdev_infos` 字段 |

---

### Phase 11: 单入口编排脚本 + artifact 处理 + 并行预初始化 ✅

**背景**：当前 `do.sh` 流程是**串行**的（primary_exit → start_secondary → wait_pre_init → secondary_takeover），Secondary 启动的 3-5 秒全部落在 IO 中断窗口内。这与设计流程图（阶段一：预初始化，Primary 仍在运行，IO 不中断）矛盾。且脚本无 artifact 处理能力，需手动指定二进制路径。

**目标**：单命令 `spdk_hot_upgrade.sh <artifact>` 完成全流程，IO 中断仅限于 Primary 挂起 → Secondary 接管的毫秒级临界区。

**实测结果**（2026-06-30）：IO 中断 **92ms**，远低于 200ms 目标。全流程 wall-clock 0.8s。

#### 11.1 新增单入口脚本 `spdk_hot_upgrade.sh` ✅

```bash
#!/usr/bin/env bash
# 单命令完成 SPDK 热替换
# Usage: spdk_hot_upgrade.sh <artifact> [options]
./scripts/hot_upgrade/spdk_hot_upgrade.sh /tmp/spdk-24.01.1.tar.gz
```

**artifact 解析逻辑**（`resolve_artifact()` 函数）：

| 输入类型 | 识别方式 | 处理 |
|---------|---------|------|
| tar.gz 包 | 后缀 `.tar.gz`/`.tgz` | 解压到 `/var/tmp/spdk_hu_new/`，`find` 定位 `spdk_tgt` |
| 目录路径 | 存在且为目录 | 按优先级查找 `build/bin/spdk_tgt` → `bin/spdk_tgt` → `find` |
| 可执行文件 | 存在且 `-x` | 直接使用 |

**编排流程**：`resolve_artifact` → `precheck.sh` → `do.sh` → `commit.sh`，任一步骤失败自动调用 `rollback.sh`。

#### 11.2 并行预初始化流程重写 ✅

重写 `spdk_hot_upgrade_do.sh` 的步骤顺序：

```
旧流程（串行，IO 中断 ~5s）：
  primary_exit → start_secondary → wait_pre_init → secondary_takeover
  └──────────────── IO 中断窗口（含 Secondary 启动）────────────────┘

新流程（并行，实测 IO 中断 92ms）：
  read_primary_params → start_secondary → wait_pre_init ──┐  Primary 正常运行
                                                          │  IO 不中断
  primary_exit → secondary_takeover → verify
  └── IO 中断窗口（仅接管）──┘
```

新步骤定义：

| 步骤 | 操作 | Primary 状态 | IO 中断 |
|------|------|-------------|---------|
| step1_read_primary_params | 从 `/proc/<pid>/cmdline` 解析 shm_id/base_virtaddr/core_mask | RUNNING | 无 |
| step2_start_secondary | 启动 Secondary（带 CLI 参数） | RUNNING | 无 |
| step3_wait_pre_init | 等待 `SECONDARY_PRE_INIT_DONE` | RUNNING | 无 |
| step4_primary_exit | 触发 `primary_exit` RPC | DRAINING → SUSPENDED | **开始** |
| step5_secondary_takeover | 触发 `secondary_init` RPC | SUSPENDED | 中断中 |
| step6_verify | 验证 Secondary 健康 + bdev 列表 | SUSPENDED | **结束** |

**关键 C 代码改动**（`lib/event/app.c:spdk_app_secondary_pre_init`）：

并行流程要求 Secondary 在 Primary 仍运行时启动，但此时 IPC socket 和状态文件尚不存在（Primary 在 `primary_exit` 时才创建）。修改 `spdk_app_secondary_pre_init` 使 IPC 连接和状态文件加载变为**非致命**：

```c
/* Connect to IPC socket (optional in parallel flow — Primary may not
 * have created it yet during pre_init; it will be available at takeover). */
rc = spdk_hot_upgrade_connect_ipc_sock();
if (rc < 0) {
    SPDK_WARNLOG("IPC connect deferred (non-fatal in parallel flow): %d\n", rc);
}

/* Load shared state file (optional in parallel flow — Primary writes it
 * during primary_exit. If unavailable, fall back to CLI params for
 * base_virtaddr and reactor_mask. State file will be re-loaded by
 * bdev_secondary_takeover during the takeover phase.) */
rc = spdk_hot_upgrade_state_load(&state);
if (rc == 0) {
    opts->base_virtaddr = state->base_virtaddr;
    if (!(opts->lcore_map || opts->reactor_mask)) {
        opts->reactor_mask = spdk_cpuset_fmt(&state->core_mask);
    }
} else {
    SPDK_WARNLOG("State file not available (using CLI params): %d\n", rc);
}
```

**启动参数自动继承**（`step1_read_primary_params`）：

从 `/proc/<primary_pid>/cmdline` 解析 `--shm-id`、`--base-virtaddr`、`-m`，并继承 `/proc/<pid>/environ` 中的 `LD_LIBRARY_PATH`。当 cmdline 中未指定时，使用 SPDK 默认值：
- `--base-virtaddr=0x200000000000`（DPDK 默认）
- `-m 0x1`（SPDK 默认单核）

#### 11.3 自动回退集成 ✅

`trap hu_rollback ERR` 捕获任一步骤失败，自动 kill Secondary + 调用 `primary_resume` RPC：

| 失败点 | 回退动作 | Primary 恢复方式 |
|--------|---------|----------------|
| step1/2（Secondary 启动失败） | kill Secondary | Primary 未受影响，继续运行 |
| step3（primary_exit 失败/超时） | 调用 `primary_resume` RPC | Primary 恢复 IO |
| step4（secondary_init 失败） | kill Secondary + 调用 `primary_resume` | Primary 恢复 IO |
| step5（验证失败） | 调用 `rollback.sh` 完整回退 | Primary 恢复或重启 |

#### 11.4 IO 中断测量 ✅

```bash
T_IO_START=$(date +%s.%N)   # step4 primary_exit 发起前
_rpc_call "$PRIMARY_SOCK" "primary_exit" "$TIMEOUT"
_rpc_call "$SECONDARY_SOCK" "secondary_init" "$TIMEOUT"
# ... verify Secondary COMPLETE ...
T_IO_END=$(date +%s.%N)     # step6 verify 通过后
IO_INTERRUPTION=$(echo "$T_IO_END - $T_IO_START" | bc)
echo "[METRIC] IO interruption: ${IO_INTERRUPTION}s (target: <0.2s)"
```

**实测输出**：`[METRIC] IO interruption: .091764546s (target: <0.2s)` — **92ms**

#### 11.5 precheck 增强 ✅

新增 3 项检查（共 8 项），替换原 `check_secondary_binary`：

| 检查项 | 函数 | 失败行为 |
|--------|------|---------|
| 二进制存在且可执行 | `check_artifact`（第 1 部分） | ERROR |
| 二进制是有效 SPDK 程序 | `check_artifact`（第 2 部分，`--version` 输出含 "SPDK"） | ERROR |
| 版本兼容性 | `check_dpdk_version`（对比 Primary RPC vs binary `--version`） | 跨大版本 ERROR，同版本 WARNING |
| Primary cmdline 可读 + `--shm-id` | `check_primary_cmdline` | `--shm-id` 缺失 ERROR，`--base-virtaddr` 缺失 WARNING |

#### 11.6 脚本文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `scripts/hot_upgrade/spdk_hot_upgrade.sh` | **新增** ✅ | 单入口：artifact 解析 + 调度 precheck/do/commit |
| `scripts/hot_upgrade/spdk_hot_upgrade_do.sh` | **重写** ✅ | 并行流程 + 自动回退 trap + IO 测量 |
| `scripts/hot_upgrade/spdk_hot_upgrade_precheck.sh` | **增强** ✅ | 8 项检查：artifact + 版本 + cmdline |
| `scripts/hot_upgrade/spdk_hot_upgrade_commit.sh` | 不变 | |
| `scripts/hot_upgrade/spdk_hot_upgrade_rollback.sh` | 不变 | |
| `scripts/hot_upgrade/test_phase11.py` | **新增** ✅ | 4 项测试：precheck 通过/拒绝 + 并行流程 + 回退 |
| `lib/event/app.c` | **修改** ✅ | `spdk_app_secondary_pre_init` IPC/状态文件非致命化 |

---

### Phase 12: IO 中断测量与优化

**目标**：量化 IO 中断时长，定位瓶颈，优化到 < 200ms。

#### 12.1 SPDK 侧高精度时间戳 ✅

在 `include/spdk/hot_upgrade_shared.h` 中新增时间记录结构，**写入独立 timeline 文件**（`/var/tmp/spdk_hot_upgrade_timeline`）而非 state 文件——避免修改 `spdk_hot_upgrade_shared_state` 结构体大小导致 `state_load` 的 `fstat` 检查失败：
```c
struct spdk_hu_timeline {
    uint64_t tsc_rate;                    // spdk_get_ticks_hz()
    uint64_t tsc_primary_exit_start;      // primary_exit RPC 进入
    uint64_t tsc_primary_drain_done;      // IO 排空完成
    uint64_t tsc_primary_suspend_done;    // Primary 挂起完成
    uint64_t tsc_secondary_init_start;    // secondary_init RPC 进入
    uint64_t tsc_secondary_takeover_done; // takeover 完成
    uint64_t tsc_reactor_running;         // Reactor 切到 RUNNING
};
```

通过 `spdk_get_ticks()` 记录（利用 SPDK 已有的高精度 TSC），Primary 创建 timeline 文件（`mmap MAP_SHARED`），Secondary 加载并追加后续时间戳。新增 `hot_upgrade_get_timeline` RPC 返回 JSON 含 6 个 TSC 字段 + 计算后的 `total_io_interruption_ms`。`do.sh` step6 查询并打印各阶段耗时分解。

**实测结果**：TSC IO 中断 14ms（远低于 200ms 阈值），3/3 测试通过。

#### 12.2 关键路径分析

| 阶段 | 耗时预期 | 优化手段 |
|------|---------|---------|
| Primary IO 排空 | < 50ms | 超时保护 5s，正常 IO 深度小 |
| 状态保存 + FD 传递 | < 20ms | 共享内存直写，SCM_RIGHTS 单次发送 |
| Secondary mmap 重放 | < 10ms | MAP_FIXED 直接映射 |
| Reactor 状态切换 | < 5ms | 原子写 `g_reactor_state` |
| io_channel 重建 | < 50ms | 按 bdev 数量线性 |
| **合计临界区** | **< 200ms** | |

#### 12.3 外部 fio 延迟监控

测试脚本中用 fio 的 `--log_lat=m` 捕获延迟尖峰：
```bash
fio --name=hot_upgrade_test --ioengine=spdk_bdev --filename=Malloc0 \
    --rw=randread --iodepth=32 --bs=4k \
    --log_interval=10 --log_lat=m --log_buf=4096
```
热替换期间的延迟尖峰窗口即为真实 IO 中断时长，与 SPDK 内部时间戳交叉验证。

#### 12.4 优化方向

- FD 传递前移：在 Secondary pre_init 阶段就完成 FD 接收（Primary 仍运行时），takeover 时仅切换 Reactor 状态
- mmap 重放前移：Guest 内存映射在 pre_init 阶段重建（需 Primary 不释放映射）
- 状态保存异步化：Primary 排空 IO 时并发写 state 文件

---

### Phase 13: 端到端集成测试

**目标**：在真实 vhost+QEMU 环境验证热替换对业务透明。

#### 13.1 IT-2: vhost-user + QEMU + fio（核心集成测试）

**环境**：Linux + DPDK + QEMU，4 个 lcore

| 步骤 | 操作 | 验证点 |
|------|------|--------|
| 1 | Primary 创建 malloc bdev + vhost-blk 控制器 | socket 文件存在 |
| 2 | QEMU 启动 VM，连接 vhost socket | VM 正常启动 |
| 3 | VM 内启动 fio（randread, iodepth=32, bs=4k） | fio 持续输出 IOPS |
| 4 | 执行 `spdk_hot_upgrade.sh <new_artifact>` | 热替换完成 |
| 5 | 监控 fio 输出 + 延迟日志 | 无错误，延迟尖峰 < 200ms |
| 6 | 热替换后 IOPS 对比 | 偏差 < 5% |
| 7 | VM 内 `dmesg` 检查 | 无磁盘断开事件 |

**成功标准**：fio 0 error + IO 中断 < 200ms + IOPS 偏差 < 5%。

#### 13.2 IT-3: 多 vhost 设备热替换

4 个 vhost-blk + 4 个 VM，同时热替换，全部 fio 无错误。

#### 13.3 IT-4: 超时回退

`primary_exit` 后不启动 Secondary → 超时 → Primary 自动 `primary_resume` 恢复 IO。

#### 13.4 IT-5: Secondary 接管失败回退

`secondary_init` 故意失败（注入错误）→ 自动 `primary_resume` → Primary 恢复 IO。

#### 13.5 ST-1: 长时间稳定性

24h 内每 2h 一次热替换（共 12 次），无内存泄漏、IOPS 无衰减。

#### 13.6 性能基准

| 指标 | 测量方法 | 目标 |
|------|---------|------|
| IO 中断时长 | fio 延迟尖峰 + SPDK 内部时间戳 | < 200ms |
| 热替换总耗时 | 脚本端到端计时 | < 10s |
| Secondary 预初始化耗时 | 日志计时（并行，不计入中断） | < 8s |
| 热替换后 IOPS 偏差 | fio 前后对比 | < 5% |

---

## 修改文件总清单

| 操作 | 文件 | 变更内容 |
|------|------|---------|
| **新增** | `include/spdk/hot_upgrade.h` | 热升级状态枚举、状态机 API |
| **新增** | `include/spdk/hot_upgrade_shared.h` | 共享状态数据结构（含全局头指针、内存映射表、conn 状态） |
| **新增** | `lib/hot_upgrade/hot_upgrade.c` | 状态机、IPC socket、状态文件读写 |
| **新增** | `lib/hot_upgrade/hot_upgrade_rpc.c` | 4 个 RPC 端点 |
| **新增** | `lib/hot_upgrade/Makefile` | 构建规则 |
| **新增** | `lib/vhost/vhost_hu.c` | FD 提取/注入、内存映射重放、epoll 清理、conn 状态恢复 |
| **新增** | `scripts/hot_upgrade/spdk_hot_upgrade_precheck.sh` | |
| **新增** | `scripts/hot_upgrade/spdk_hot_upgrade_do.sh` | |
| **新增** | `scripts/hot_upgrade/spdk_hot_upgrade_commit.sh` | |
| **新增** | `scripts/hot_upgrade/spdk_hot_upgrade_rollback.sh` | |
| **修改** | `lib/Makefile` | 添加 hot_upgrade 子目录 |
| **修改** | `include/spdk/init.h` | subsystem 扩展 4 个新回调 + 框架 API |
| **修改** | `lib/init/subsystem.c` / `lib/init/subsystem.h` | 四个遍历函数 |
| **修改** | `include/spdk/event.h` | `spdk_app_secondary_pre_init/full_init` 声明 |
| **修改** | `lib/event/app.c` | Secondary 启动流程、Primary exit 流程 |
| **修改** | `lib/event/reactor.c` | **新增 SPDK_REACTOR_STATE_IDLE** + usleep 轮询 |
| **修改** | `lib/vhost/vhost_internal.h` | `spdk_vhost_dev` 扩展 |
| **修改** | `lib/vhost/vhost.c` | **`strdup() → spdk_dma_malloc()`** |
| **修改** | `lib/vhost/rte_vhost_user.c` | **`posix_memalign() → spdk_zmalloc(DMA)`** |
| **修改** | `module/event/subsystems/vhost_blk/vhost_blk.c` | 四阶段热替换回调 |
| **修改** | `module/event/subsystems/vhost_scsi/vhost_scsi.c` | 四阶段热替换回调 |
| **修改** | `module/event/subsystems/bdev/bdev.c` | 四阶段热替换回调 |
| **修改** | `module/bdev/bdev.c` | **`calloc() → spdk_zmalloc(DMA)`** + channel 重建 |
| **修改** | `mk/spdk.lib.mk` | hot_upgrade 库链接 |

### 相比原方案的关键变更

| 项目 | 说明 |
|------|------|
| ❌ 废弃 JSON 回放 | 改为共享数据结构指针继承 |
| ❌ 废弃位图追踪器 | Primary 挂起不释放内存 |
| ✅ 新增 Guest 内存 mmap 重放 | 解决 GPA→VVA 映射失效 |
| ✅ 新增 DPDK rte_vhost 内部状态恢复 | 解决 vid 上下文断层 |
| ✅ 新增 Reactor IDLE 模式 | 利用现有 g_reactor_state |
| ✅ 新增 Primary epoll 清理 | 防止 Ghost kick 唤醒 |
| ✅ 强制 bdev/vhost 对象 DPDK DMA 分配 | 保证跨进程可见 |

---

## 编码阶段注意事项（POC 防坑指南）

以下是代码级实现的 5 个"暗坑"，均已在 SPDK 代码仓库中验证确认。POC 阶段务必提前处理。

### 暗坑1：DPDK rte_vhost 内部锁的死锁风险

**风险点**：

- `g_vhost_mutex`（`lib/vhost/vhost.c:20`）：保护全局 `g_vhost_devices` RB 树
- `user_dev->lock`（`lib/vhost/vhost_internal.h:187`）：per-device 保护 `vsessions` 链表
- `vsession->dpdk_sem`（`lib/vhost/vhost_internal.h:163`）：per-session 同步 DPDK 回调

若 Primary 的 `vhost_blk_primary_suspend()` 在持有 `user_dev->lock` 时调用 `pause()`，Secondary 的 `vhost_blk_secondary_takeover()` 中任何尝试 `pthread_mutex_lock(&user_dev->lock)` 的操作将永久阻塞。

**防御措施**：Primary 挂起前释放所有 vhost 内部锁。

```c
void vhost_blk_primary_suspend(void)
{
    for (vdev = spdk_vhost_dev_next(NULL); vdev != NULL;
         vdev = spdk_vhost_dev_next(vdev)) {
        struct spdk_vhost_user_dev *user_dev = to_user_dev(vdev);

        // 确保不持有任何 DPDK vhost 锁
        assert(spdk_vhost_trylock() == 0);
        spdk_vhost_unlock();

        pthread_mutex_lock(&user_dev->lock);
        // 提取状态、停止 poller、清理 epoll
        pthread_mutex_unlock(&user_dev->lock);  // 必须在 pause() 前释放！
    }
    spdk_subsystem_primary_suspend_next(0);
}
```

### 暗坑2：spdk_io_channel 与 spdk_thread 的强绑定

**风险点**：

- `spdk_get_io_channel()` 内部断言 `spdk_get_thread()`（`lib/thread/thread.c`）
- 每个 channel 通过 `RB_INSERT(&thread->io_channels, ...)` 存入对应 thread 的 RB 树
- 若在 RPC handler 线程（非 Reactor 线程）上调用 `spdk_bdev_get_io_channel()` → **断言失败**

**防御措施**：先设置 Reactor RUNNING，再通过 `spdk_thread_send_msg` 在 Reactor 线程上下文中创建 channel。

```c
void vhost_blk_secondary_takeover(void)
{
    // 接收 FD、mmap 重放、恢复 vid 上下文（在 RPC 线程中完成）
    spdk_vhost_recv_fds_from_primary(vdev);
    spdk_vhost_restore_mem_mapping(vsession);
    spdk_vhost_restore_connections(vdev);

    // 先设置 Reactor RUNNING
    g_reactor_state = SPDK_REACTOR_STATE_RUNNING;

    // 在 Reactor 线程中创建 channel
    spdk_thread_send_msg(reactor_thread, _takeover_create_channels, vdev);
}

static void _takeover_create_channels(void *ctx)
{
    // 此时已在 Reactor 线程上下文，可安全创建 channel
    for (each bdev) {
        struct spdk_io_channel *ch = spdk_bdev_get_io_channel(bdev_desc);
    }
    spdk_vhost_register_poller(vsession);
}
```

### 暗坑3：MAP_FIXED 的虚拟地址空间冲突

**风险点**：

- DPDK rte_vhost 调用 `mmap(NULL, ...)` 让内核随机选择 Guest 内存映射地址
- Secondary 进程中该地址可能已被 `.so` 或栈占用 → `MAP_FIXED` 将覆盖已有映射

**防御措施**：从预留的虚拟地址空间分配，确保主备地址一致。

```c
#define SPDK_HU_GUEST_MEM_BASE_OFFSET  (256ULL * 1024 * 1024 * 1024)  // 紧跟 DPDK 区域

void *spdk_vhost_reserve_guest_mem_addr(size_t size)
{
    uint64_t addr = spdk_env_get_base_virtaddr() + SPDK_HU_GUEST_MEM_BASE_OFFSET;
    // 使用 MAP_FIXED_NOREPLACE 安全检测冲突
    void *ret = mmap((void *)addr, size,
                     PROT_NONE, MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED_NOREPLACE, -1, 0);
    if (ret == MAP_FAILED) {
        return NULL;
    }
    return ret;
}
```

### 暗坑4：Secondary 的 RPC Server 地址冲突

**风险点**：

- `include/spdk/init.h:23`：`#define SPDK_DEFAULT_RPC_ADDR "/var/tmp/spdk.sock"`
- Primary 已占用 `/var/tmp/spdk.sock`

**防御措施**：Secondary 启动时使用不同路径。

```bash
./new_spdk/build/bin/spdk_tgt \
    --shm-id=0 --base-virtaddr=0x200000000000 -m 0x3 \
    --proc-type=secondary \
    -r /var/tmp/spdk_secondary.sock &
```

共享状态文件中记录 Secondary RPC 路径：

```c
struct spdk_hot_upgrade_shared_state {
    char primary_rpc_addr[108];
    char secondary_rpc_addr[108];   // 编排器从此字段获取 Secondary RPC 地址
};
```

### 暗坑5：Primary 挂起前缺少全内存屏障

**风险点**：

- CPU 乱序执行可能导致共享内存修改仍在 Store Buffer 中
- Secondary 读取到旧值 → **指针飞起、段错误**

**防御措施**：挂起前插入全内存屏障。

```c
static void app_primary_exit(void)
{
    spdk_subsystem_primary_drain_io();
    spdk_hot_upgrade_state_save();      // 写入全局头指针
    spdk_subsystem_primary_suspend();   // FD 传递 + 锁释放

    // 全内存屏障：确保所有写入对 Secondary 可见
    __sync_synchronize();
    // 或使用 DPDK 版本：rte_mb();

    spdk_hot_upgrade_set_state(SPDK_HU_PRIMARY_SUSPENDED);

    while (spdk_hot_upgrade_get_state() == SPDK_HU_PRIMARY_SUSPENDED) {
        pause();
    }
    exit(0);
}
```

### 暗坑6：DPDK rte_vhost 中 Guest 内存 fd 的生命周期

**风险点**：

- 标准 DPDK rte_vhost 在完成 `VHOST_USER_SET_MEM_TABLE` 协议的 `mmap` 后，可能会调用 `close(fd)` 释放文件描述符
- Primary 的 `spdk_vhost_send_mem_fds()` 提取 `mem->regions[i].fd` 时，该 fd 可能已被 DPDK 关闭
- SCM_RIGHTS 传递无效 fd 给 Secondary → `mmap` 返回 `EBADF`

**防御措施**：在 mmap 成功后不要 close fd，或 Primary 在热替换触发时通过 `dup()` 创建新 fd。

```c
// 方案1：修改 DPDK rte_vhost SET_MEM_TABLE 处理，保留 fd 不关闭
// 在 vhost_user_set_mem_table() 中：
// 成功 mmap 后，不调用 close(fd)

// 方案2：Primary 在发送前重新打开 fd
// 使用 /proc/self/mem 或遍历 /proc/self/fd/<N> 反查 mem_region 对应的 fd
// 然后 dup() 生成新 fd 供传递
void spdk_vhost_send_mem_fds_v2(struct spdk_vhost_dev *vdev)
{
    TAILQ_FOREACH(vsession, &to_user_dev(vdev)->vsessions, tailq) {
        struct rte_vhost_memory *mem = vsession->mem;
        for (uint32_t i = 0; i < mem->nregions; i++) {
            int fd;

            // 检查原 fd 是否有效
            if (fcntl(mem->regions[i].fd, F_GETFD) == -1 && errno == EBADF) {
                // fd 已被 DPDK 关闭，从 /proc/self/mem 反弹获取
                fd = spdk_hot_upgrade_recover_mem_fd(
                    mem->regions[i].mmap_addr, mem->regions[i].mmap_size);
            } else {
                fd = dup(mem->regions[i].fd);  // 复制一个有效引用
            }
            // 发送 fd 给 Secondary
            send_fd(ipc_sock, fd);
            // 发送后在 Primary 中 close dup 的副本
            close(fd);
        }
    }
}
```

**关键规则**：SCM_RIGHTS 发送前必须 `fcntl(F_GETFD)` 检查 fd 有效性。发送完成后 Secondary 端立即 `mmap` 持有该 fd 引用，Primary 端可安全关闭 `dup` 副本。

### 暗坑7：共享对象指针重定向 vs 值拷贝

**风险点**：

- 当前 `g_bdev_mgr` 是静态全局变量（`.bss` 段），内部包含 `spdk_spinlock`（`lib/bdev/bdev.c:572`、`:573`）
- 如果做值拷贝：`g_bdev_mgr = *(struct spdk_bdev_mgr *)state->bdev_mgr_addr;`
  - `spdk_spinlock` 的内部状态（如 `pthread_spinlock_t`）被拷贝到 Secondary 私有段
  - Secondary 获得的锁状态是 Primary 锁的中间状态快照 → 状态错乱
  - 结构体内若有指向进程私有堆的指针 → 野指针

**防御措施**：将全局变量改为指针，Primary 和 Secondary 通过指针访问共享内存中同一个结构体实例。

```c
// Phase 0.3 修正版：g_bdev_mgr 由静态全局变量改为指针
// 修改前（lib/bdev/bdev.c）：
static struct spdk_bdev_mgr g_bdev_mgr;  // ❌ 静态全局变量

// 修改后：
static struct spdk_bdev_mgr *g_bdev_mgr = NULL;  // ✅ 指针

void bdev_subsystem_init(void)
{
    // Primary 分配在共享大页中
    g_bdev_mgr = spdk_zmalloc(sizeof(*g_bdev_mgr), 0, NULL,
                              SPDK_ENV_SOCKET_ID_ANY, SPDK_MALLOC_DMA);
    spdk_spin_init(&g_bdev_mgr->spinlock);
    ...
}

// Primary 挂起前：保存指针值
state->bdev_mgr_addr = (uint64_t)g_bdev_mgr;  // 保存指针，而非 *g_bdev_mgr

// Secondary 启动时：直接使用指针
void bdev_subsystem_secondary_pre_init(void)
{
    struct spdk_hot_upgrade_shared_state *state = spdk_hot_upgrade_state_load();
    g_bdev_mgr = (struct spdk_bdev_mgr *)state->bdev_mgr_addr;
    // ✅ 现在 Primary 和 Secondary 操作的是共享内存中同一个 g_bdev_mgr 实例
    // ✅ spinlock 状态一致
    spdk_subsystem_secondary_pre_init_next(0);
}
```

**同样适用于** `g_vhost_devices`（RB_ROOT）、`g_spdk_event_mempool` 等全局管理结构。

### 已验证安全：spdk_reactors_start() 与 IDLE 模式的兼容性

**验证结果**：

- `lib/event/reactor.c:1097-1129`：`spdk_reactors_start()` 主线程进入 `reactor_run()` 阻塞，但 **Reactor 线程独立运行**
- `reactor_run()` 内部 `while(1)` 循环通过 `event_queue_run_batch()` 持续处理 RPC 消息
- IDLE 模式下 `while` 循环不退出，持续调用 `event_queue_run_batch() + usleep(1000)`

**结论**：当前方案时序正确，无需修改。Secondary 在 `spdk_app_secondary_pre_init()` 最后一步调用 `spdk_reactors_start()` 即可，RPC 通道畅通。

### POC 最小验证三步骤

在铺开全量代码之前，先搭建最小验证环境：

```bash
# 测试1：共享内存可见性
# Primary 中 spdk_zmalloc(DMA) 一个结构体 → Secondary lookup 并读取

# 测试2：mmap 重放（含 fd 有效性检查）
# Primary 中 mmap 一块内存 → fcntl(F_GETFD) 检查 fd → 传递 → Secondary mmap 并读写

# 测试3：Reactor IDLE→RUNNING 状态切换
# Secondary 以 IDLE 启动 → RPC 收到命令 → 切换为 RUNNING

# 测试4：指针重定向验证
# Primary 中 g_bdev_mgr 指针指向共享内存 → Secondary 读出相同指针值直接访问
```

---

## 验证计划

### 单元测试

1. **Reactor IDLE 模式测试**：`g_reactor_state=IDLE` 下 usleep + RPC 处理
2. **Guest 内存 mmap 重放测试**：相同虚拟地址 MAP_FIXED 映射验证
3. **状态文件读写测试**：全局头指针、内存映射表读写一致性
4. **IPC socket FD 传递测试**：SCM_RIGHTS 多 FD 传递
5. **锁释放安全测试**：挂起前后线程锁持有的 assert 检查
6. **io_channel 创建线程正确性测试**：非 Reactor 线程创建 channel 应断言失败

### 集成测试

```bash
# 1. Primary 启动
./build/bin/spdk_tgt --shm-id=0 --base-virtaddr=0x200000000000 -m 0x3 &

# 2. 配置
./scripts/rpc.py bdev_malloc_create 64 512 -b Malloc0
./scripts/rpc.py vhost_create_blk_controller --cpumask 0x1 vhost.0 Malloc0

# 3. QEMU + fio
qemu-system-x86_64 ... -chardev socket,id=char0,path=./vhost.0 ...

# 4. 热替换
./scripts/hot_upgrade/spdk_hot_upgrade_precheck.sh
./scripts/hot_upgrade/spdk_hot_upgrade_do.sh
./scripts/hot_upgrade/spdk_hot_upgrade_commit.sh
```

---

## 风险与缓解

| 风险 | 等级 | 缓解策略 |
|------|------|---------|
| bdev/vhost 对象不在共享内存（已验证） | **高** | 修改 `posix_memalign/strdup/calloc` → `spdk_zmalloc(DMA)` |
| Guest 内存 mmap 进程私有（已验证） | **高** | MAP_FIXED 在相同虚拟地址重建映射 |
| DPDK rte_vhost vid 上下文断层（已验证） | **高** | 共享内存传递 conn 状态 + DPDK 补丁恢复 vid 上下文 |
| Reactor 无 IDLE 模式（已验证） | **中** | 已有 g_reactor_state 机制，新增 IDLE 枚举值 |
| Primary epoll 残留（已验证） | **中** | 挂起前清除 DPDK fdset epoll 的 kickfd 注册 |
| In-flight IO 丢失 | **高** | Primary 先排空 IO 再进行 FD 传递，超时保护 5 秒 |
| 核心 mask 不一致导致 cache miss | **中** | 强制 Primary/Secondary 使用相同 `-m` 参数 |
| spdk_io_channel 不能跨进程 | **高** | Secondary 在新 spdk_thread 上通过 `spdk_bdev_get_io_channel()` 重建 |
| DPDK 版本不兼容 | **低** | 禁止跨 DPDK 版本热升级，预检查阶段验证 |

---

## 时间线规划

| Phase | 内容 | 预计工作量 |
|-------|------|-----------|
| Phase 0 | 强制共享分配修改 + 内存验证 | 3-5 天 | ✅ 完成 |
| Phase 1 | 状态机 + IPC socket | 2-3 天 | ✅ 完成 |
| Phase 2 | Subsystem 接口扩展 | 2-3 天 | ✅ 完成 |
| Phase 3 | App 框架 + Reactor IDLE 改造 | 3-5 天 | ✅ 完成 |
| Phase 4 | vhost FD 传递 + Guest mmap 重放（核心难点） | **8-12 天** | ✅ 完成 |
| Phase 5 | 共享状态文件（精简版） | 2-3 天 | ✅ 完成 |
| Phase 6 | RPC 端点 | 2-3 天 | ✅ 完成 |
| Phase 7 | vhost 子系统适配 | 4-6 天 | ✅ 完成 |
| Phase 8 | bdev 子系统适配 | 2-3 天 | ✅ 完成 |
| Phase 9 | 编排脚本 | 2-3 天 | ✅ 完成 |
| Phase 10 | bdev 共享实现（TAILQ 头传递 + PIE fixup） | 3-5 天 | ✅ 完成 |
| Phase 11 | 单入口编排脚本 + artifact + 并行预初始化 | 4-6 天 | ✅ 完成 |
| Phase 12-13a | IO 中断测量 + bdev 重建功能测试 | 3-5 天 | ✅ 完成（commit `84686b35c`，合并提交） |
| Phase 13b | 端到端集成测试（QEMU+vhost+fio） | 5-7 天 | ⚪ 待开始 |
| **合计** | | **30-45 天（v1）+ 14-21 天（v2）** | **v1 完成：25文件编译通过；v2 待实现** |

---

## POC 验证结果（截至 2026-06-29）

### 编译验证 ✅

- 远端 SPDK 24.01 (git 0786843) + DPDK 23.11.0，全部 25 文件编译通过
- 17 个热升级符号正确导出（spdk_init.map / spdk_event.map / spdk_vhost.map）
- spdk_tgt 二进制产出正常，仅 1 个预存类型警告（spdk_event_allocate，非阻塞）

### Primary 流程 ✅

| 步骤 | 结果 |
|------|:---:|
| Primary 启动（`--shm-id=100`） | ✅ state=IDLE |
| `primary_exit` RPC | ✅ IDLE→DRAINING→SUSPENDED |
| Reactor HU_PAUSED 模式 | ✅ RPC 线程仍可响应（替代 pause() 阻塞） |
| state_save | ✅ 共享状态文件生成 |
| create_ipc_sock | ✅ IPC 监听 socket 生成 |

### Secondary 流程 ✅

| 步骤 | 结果 |
|------|:---:|
| Secondary 启动（`--proc-type=secondary`） | ✅ 进程存活 |
| DPDK EAL 初始化（`--proc-type=auto` 连接共享大页） | ✅ |
| spdk_reactors_init（msg_mempool 创建） | ✅ |
| Secondary RPC 响应（`spdk_get_version`） | ✅ 返回 v24.01 |
| Secondary HU 状态 | ✅ `SECONDARY_PRE_INIT_DONE` |

### Takeover 流程 ✅

| 步骤 | 结果 |
|------|:---:|
| Primary 创建 malloc bdev | ✅ Malloc0 |
| primary_exit → Primary SUSPENDED | ✅ |
| Secondary pre_init → SECONDARY_PRE_INIT_DONE | ✅ |
| `secondary_init` RPC → 状态 COMPLETE | ✅ |
| Secondary reactor → RUNNING | ✅ |
| Secondary bdev 子系统可用（可创建新 bdev） | ✅ SecMalloc0 |
| Primary bdev 共享给 Secondary | ✅ 已共享（TAILQ 头传递 + PIE fixup，Phase 10 完成） |
| Primary/Secondary 共存（Primary HU_PAUSED, Secondary RUNNING） | ✅ |

**测试脚本**：`scripts/hot_upgrade/test_takeover.py`
**结论**：热升级状态机全流程验证通过（IDLE→DRAINING→SUSPENDED→SECONDARY_PRE_INIT→SECONDARY_PRE_INIT_DONE→TAKEOVER→COMPLETE）。Secondary 接管后 reactor 正常运行、bdev 子系统可用。Phase 10 bdev 共享已实现：Primary 的 bdev 通过 TAILQ 头传递 + PIE fixup 传递给 Secondary，`bdev_get_bdevs` 正确返回 Malloc0。

### 调试中发现并修复的关键缺陷

| # | 缺陷 | 文件 | 修复 |
|---|------|------|------|
| D2 | Secondary `reactor_mask=NULL` → EAL 拒绝 | app.c | 从 state core_mask 经 `spdk_cpuset_fmt` 恢复 |
| D3 | Secondary `msg_mempool_size=0` → ENOMEM | app.c | 补充 `SPDK_DEFAULT_MSG_MEMPOOL_SIZE` 默认值 |
| D5 | `create_ipc_sock()` 定义但从未调用 | hot_upgrade_rpc.c | 在 primary_drain_io_done 中调用 |
| D6 | state_load `O_RDONLY` + mmap `PROT_WRITE` → EACCES | hot_upgrade.c | 改为 `O_RDWR` |
| D7 | `--proc-type=secondary` SPDK parser 不识别 | spdk_tgt.c | argv 剥离后再 parse_args |
| D8 | Secondary 未调 `hot_upgrade_init` + IPC fd 误判 | app.c | 补 init + `rc<0` 检查 |
| D9 | Secondary 未调 `app_setup_env` → 无 DPDK 内存 | app.c | 补 env 初始化 |

### 未完成项（v2 计划，2026-06-29 更新）

| 内容 | 优先级 | 对应 Phase | 说明 |
|------|:---:|:---:|------|
| bdev 共享（TAILQ 头传递 + PIE fixup） | ✅ | Phase 10 ✅ | TAILQ 头值传递 + fn_table/module/aliases/qos fixup；Phase 13a 改用重建方案（secondary_reconstruct 回调） |
| 单入口脚本 `spdk_hot_upgrade.sh` | ✅ | Phase 11 ✅ | artifact 解析（tar/路径/版本号）+ 单命令调度 |
| 并行预初始化（IO 中断最小化） | ✅ | Phase 11 ✅ | 重写 `do.sh`：Secondary 先 pre_init 再 primary_exit |
| 自动回退集成 | ✅ | Phase 11 ✅ | 任一步骤失败自动 `primary_resume` |
| IO 中断测量 | ✅ | Phase 12-13a ✅ | SPDK 侧 TSC 时间戳（独立 timeline 文件）+ hot_upgrade_get_timeline RPC；commit `84686b35c` |
| bdev 层热替换功能测试 | ✅ | Phase 12-13a ✅ | 方案 B（TCG 模式）：bdev 重建验证通过，Malloc0 参数一致；commit `84686b35c`，远端复验 8/8 PASS（2026-07-02）。**2026-07-02 修复 accel/iobuf 子系统 secondary_pre_init 缺失导致 bdev_register 崩溃**：Secondary 重建 bdev 时 `spdk_bdev_register` → `spdk_accel_get_opc_memory_domains` 解引用 NULL `g_modules_opc[opcode].module`。为 iobuf 和 accel 子系统添加 `secondary_pre_init` 回调（拓扑序 iobuf→accel→bdev），修复后真实 DPDK 多进程热替换测试通过（TSC 137ms，COMPLETE，Malloc0 bs=512 nb=131072 重建成功） |
| QEMU+vhost+fio 集成测试 | 🔴 | Phase 13b | 需安装 QEMU + fio + VM 镜像，验证 vhost 端到端 IO |
| 多 vhost 设备 + 长时间稳定性测试 | ⚪ | Phase 13 | IT-3 / ST-1 |
| C-12: trace 点 | ⚪ | — | Low |
| C-14: JSON RPC schema | ⚪ | — | Low |
| C-15: 单元测试 | ⚪ | — | Low |
| P4-02: 数组超限处理 | ⚪ | — | Low |

### v2 实施顺序建议

```
Phase 10 (bdev 共享)  ──►  Phase 11 (脚本整合 + 并行预初始化)
                                    │
                                    ▼
                          Phase 12-13a (IO 测量 + bdev 重建测试)  [commit 84686b35c]
                                    │
                                    ▼
                          accel/iobuf secondary_pre_init 修复 (2026-07-02)
                          真实 DPDK 多进程热替换测试通过 (TSC 137ms)
                                    │
                                    ▼
                          全新克隆编译测试 (2026-07-03, GitHub master_hot_replace)
                          Phase 12 全部通过: TSC 52-132ms (目标 <200ms ✅)
                                    │
                                    ▼
                          Phase 13b (QEMU 端到端测试)
```

**关键路径**：Phase 10 → Phase 11 是交付关键路径。Phase 10 解决 bdev 共享后 takeover 才真正完整；Phase 11 实现并行预初始化后 IO 中断才能降到毫秒级。Phase 12-13a 已完成 IO 测量与 bdev 重建功能验证（合并提交 `84686b35c`），Phase 13b 为 QEMU 端到端测试。
