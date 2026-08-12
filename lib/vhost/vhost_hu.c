/*   SPDX-License-Identifier: BSD-3-Clause
 *   Copyright (C) 2024 SPDK Hot Upgrade Contributors.
 *   All rights reserved.
 */

#include "spdk/stdinc.h"
#include "spdk/env.h"
#include "spdk/hot_upgrade.h"
#include "spdk/hot_upgrade_shared.h"
#include "spdk/init.h"
#include "spdk/log.h"
#include "spdk/string.h"
#include "spdk/thread.h"
#include "spdk/util.h"
#include "spdk/vhost.h"
#include "vhost_internal.h"

#include <sys/socket.h>
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>
#include <errno.h>

/*
 * ===== Primary Side: FD Extraction and Sending =====
 */

int
spdk_vhost_send_fds_to_secondary(struct spdk_vhost_dev *vdev)
{
	struct spdk_vhost_user_dev *user_dev = to_user_dev(vdev);
	struct spdk_vhost_session *vsession;
	int ipc_sock;
	int i;

	ipc_sock = spdk_hot_upgrade_get_ipc_sock();
	if (ipc_sock < 0) {
		SPDK_ERRLOG("IPC socket not connected for FD transfer\n");
		return -ENOTCONN;
	}

	TAILQ_FOREACH(vsession, &user_dev->vsessions, tailq) {

		/* Send vhost socket fd */
		SPDK_INFOLOG(vhost, "Sending FDs for session %s (vid=%d)\n",
			     vsession->name, vsession->vid);

		for (i = 0; i < (int)vsession->max_queues; i++) {
			int kickfd = vsession->virtqueue[i].vring.kickfd;
			int rc;

			if (kickfd <= 0) {
				continue;
			}

			rc = spdk_hot_upgrade_send_fd(ipc_sock, kickfd);
			if (rc < 0) {
				SPDK_ERRLOG("Failed to send kickfd for vq %d\n", i);
				return rc;
			}

			SPDK_INFOLOG(vhost, "Sent kickfd %d for vq %d\n", kickfd, i);
		}
	}

	return 0;
}

void
spdk_vhost_cleanup_epoll_before_pause(struct spdk_vhost_dev *vdev)
{
	struct spdk_vhost_user_dev *user_dev = to_user_dev(vdev);
	struct spdk_vhost_session *vsession;
	int i;

	TAILQ_FOREACH(vsession, &user_dev->vsessions, tailq) {
		/*
		 * Mark session as not started. The vring descriptor table
		 * (desc/avail/used rings) resides in DPDK hugepage shared
		 * memory and MUST NOT be modified - Secondary will inherit
		 * these at the same virtual addresses via --base-virtaddr.
		 *
		 * Only the epoll registration is cleaned up; the actual
		 * vring state is preserved for Secondary takeover.
		 *
		 * P6-06 FIX: Removed vq->vring.desc = NULL which was
		 * destroying shared hugepage vring state.
		 */
		vsession->started = false;
		SPDK_INFOLOG(vhost, "Stopped session %s for hot upgrade\n",
			     vsession->name);
	}
}

int
spdk_vhost_extract_mem_fds(struct spdk_vhost_dev *vdev)
{
	struct spdk_vhost_user_dev *user_dev = to_user_dev(vdev);
	struct spdk_vhost_session *vsession;
	struct spdk_hot_upgrade_shared_state *state;
	int ipc_sock;

	ipc_sock = spdk_hot_upgrade_get_ipc_sock();
	if (ipc_sock < 0) {
		SPDK_ERRLOG("IPC socket not connected\n");
		return -ENOTCONN;
	}

	TAILQ_FOREACH(vsession, &user_dev->vsessions, tailq) {
		struct rte_vhost_memory *mem = vsession->mem;
		uint32_t i;

		if (mem == NULL) {
			continue;
		}

		for (i = 0; i < mem->nregions; i++) {
			int fd = mem->regions[i].fd;

			/*
			 * Check if fd is still valid. DPDK rte_vhost may have
			 * closed it after mmap. If invalid, try to recover.
			 */
			if (fcntl(fd, F_GETFD) == -1 && errno == EBADF) {
				SPDK_WARNLOG("Guest memory fd %d is invalid, "
					     "DPDK may have closed it\n", fd);
				/*
				 * TODO: Implement fd recovery via /proc/self/mem
				 * For now, skip this region.
				 */
				continue;
			}

			/* dup() to get a fresh fd that Secondary can own */
			fd = dup(mem->regions[i].fd);
			if (fd < 0) {
				SPDK_ERRLOG("Failed to dup mem fd: %s\n",
					    spdk_strerror(errno));
				continue;
			}

			if (spdk_hot_upgrade_send_fd(ipc_sock, fd) < 0) {
				SPDK_ERRLOG("Failed to send mem fd\n");
				close(fd);
				continue;
			}

			/* Primary can close the dup'd fd now */
			close(fd);

			SPDK_INFOLOG(vhost,
				     "Sent Guest memory fd for region %u "
				     "(addr=0x%lx, size=0x%lx)\n",
				     i, mem->regions[i].mmap_addr,
				     mem->regions[i].mmap_size);
		}
	}

	return 0;
}

int
spdk_vhost_extract_conn_state(struct spdk_vhost_dev *vdev)
{
	struct spdk_vhost_user_dev *user_dev = to_user_dev(vdev);
	struct spdk_vhost_session *vsession;

	TAILQ_FOREACH(vsession, &user_dev->vsessions, tailq) {
		SPDK_INFOLOG(vhost,
			     "Extracting conn state for session %s "
			     "(vid=%d, features=0x%lx)\n",
			     vsession->name, vsession->vid,
			     vsession->negotiated_features);
		/*
		 * The connection state is already in shared memory.
		 * Primary's rte_vhost internal state (vid context, features,
		 * memory table) resides in DPDK hugepages and will be
		 * inherited by Secondary via --base_virtaddr.
		 */
	}

	return 0;
}

/*
 * ===== Secondary Side: FD Reception and Injection =====
 */

int
spdk_vhost_recv_fds_from_primary(struct spdk_vhost_dev *vdev)
{
	struct spdk_vhost_user_dev *user_dev = to_user_dev(vdev);
	struct spdk_vhost_session *vsession;
	int ipc_sock;
	int i;

	ipc_sock = spdk_hot_upgrade_get_ipc_sock();
	if (ipc_sock < 0) {
		SPDK_ERRLOG("IPC socket not connected\n");
		return -ENOTCONN;
	}

	TAILQ_FOREACH(vsession, &user_dev->vsessions, tailq) {

		for (i = 0; i < (int)vsession->max_queues; i++) {
			int fd;

			fd = spdk_hot_upgrade_recv_fd(ipc_sock);
			if (fd < 0) {
				SPDK_ERRLOG("Failed to receive FD for vq %d\n",
					    i);
				return -EIO;
			}

			vsession->virtqueue[i].vring.kickfd = fd;

			SPDK_INFOLOG(vhost,
				     "Received kickfd %d for session %s vq %d\n",
				     fd, vsession->name, i);
		}
	}

	return 0;
}

int
spdk_vhost_restore_mem_mapping(struct spdk_vhost_session *vsession)
{
	struct spdk_hot_upgrade_shared_state *state;
	int ipc_sock;
	uint32_t i;

	ipc_sock = spdk_hot_upgrade_get_ipc_sock();
	if (ipc_sock < 0) {
		SPDK_ERRLOG("IPC socket not connected\n");
		return -ENOTCONN;
	}

	if (spdk_hot_upgrade_state_load(&state) != 0) {
		SPDK_ERRLOG("Failed to load shared state for mem restore\n");
		return -EINVAL;
	}

	/*
	 * Restore each Guest memory region mapping using MAP_FIXED
	 * to ensure the same virtual address as Primary.
	 */
	for (i = 0; i < state->num_mem_regions; i++) {
		int fd;
		void *addr;

		fd = spdk_hot_upgrade_recv_fd(ipc_sock);
		if (fd < 0) {
			SPDK_ERRLOG("Failed to receive mem fd for region %u\n",
				    i);
			return -EIO;
		}

		addr = mmap((void *)state->mem_regions[i].mmap_addr,
			    state->mem_regions[i].mmap_size,
			    PROT_READ | PROT_WRITE,
			    MAP_FIXED | MAP_SHARED,
			    fd, 0);

		close(fd);  /* mmap holds the reference now */

		if (addr == MAP_FAILED) {
			SPDK_ERRLOG("Failed to restore mem mapping at %p: %s\n",
				    (void *)state->mem_regions[i].mmap_addr,
				    spdk_strerror(errno));
			return -errno;
		}

		if (addr != (void *)state->mem_regions[i].mmap_addr) {
			SPDK_ERRLOG("mmap returned wrong address: %p != %p\n",
				    addr,
				    (void *)state->mem_regions[i].mmap_addr);
			return -EINVAL;
		}

		SPDK_INFOLOG(vhost,
			     "Restored Guest memory mapping at %p (size=0x%lx)\n",
			     addr, state->mem_regions[i].mmap_size);
	}

	return 0;
}

int
spdk_vhost_restore_connections(struct spdk_vhost_dev *vdev)
{
	struct spdk_vhost_user_dev *user_dev = to_user_dev(vdev);
	struct spdk_vhost_session *vsession;

	/*
	 * When base_virtaddr is identical between Primary and Secondary,
	 * the DPDK rte_vhost internal connection state (vhost_devices array,
	 * connection features, memory table) is directly accessible through
	 * the shared hugepage memory.
	 *
	 * All vring structures (desc, avail, used) are at the same virtual
	 * addresses in Secondary, and the GPA→VVA translations are valid
	 * after Guest memory mappings have been restored.
	 *
	 * P6-02: Do NOT call DPDK rte_vhost APIs that may trigger
	 * start_device callbacks. The device is already started by Primary;
	 * Secondary only needs to restore the I/O path (kickfd + poller).
	 */
	TAILQ_FOREACH(vsession, &user_dev->vsessions, tailq) {
		SPDK_INFOLOG(vhost,
			     "Connection restored for session %s (vid=%d)\n",
			     vsession->name, vsession->vid);
	}

	return 0;
}

int
spdk_vhost_session_attach_fds(struct spdk_vhost_session *vsession,
			      int sock_fd, int *kick_fds, uint16_t num_queues)
{
	uint16_t i;

	if (vsession == NULL || kick_fds == NULL) {
		SPDK_ERRLOG("Invalid args to spdk_vhost_session_attach_fds\n");
		return -EINVAL;
	}

	/*
	 * The vring structures (desc/avail/used rings) are already at valid
	 * virtual addresses in Secondary because:
	 *   1. --base-virtaddr ensures identical DPDK hugepage mappings
	 *   2. Guest memory has been restored via mmap MAP_FIXED
	 */

	for (i = 0; i < num_queues; i++) {
		struct spdk_vhost_virtqueue *vq = &vsession->virtqueue[i];

		if (kick_fds[i] <= 0) {
			continue;
		}

		vq->vring.kickfd = kick_fds[i];
		vq->vsession = vsession;
		vq->vring_idx = i;

		SPDK_INFOLOG(vhost,
			     "Attached kickfd %d for session %s vq %u\n",
			     kick_fds[i], vsession->name, i);
	}

	return 0;
}

SPDK_LOG_REGISTER_COMPONENT(vhost_hu)

/*
 * ===== In-flight IO Drain Poller =====
 */

#define HU_DRAIN_TIMEOUT_SEC 5

enum hu_drain_state {
	HU_DRAIN_IDLE = 0,
	HU_DRAIN_IN_PROGRESS,
	HU_DRAIN_DONE,
};

static enum hu_drain_state g_hu_drain_state = HU_DRAIN_IDLE;
static struct spdk_poller *g_hu_drain_poller = NULL;
static uint64_t g_hu_drain_start_tsc = 0;

static int
vhost_hu_drain_poller(void *arg)
{
	struct spdk_vhost_dev *vdev;
	struct spdk_vhost_session *vsession;
	bool all_drained = true;
	uint64_t now = spdk_get_ticks();
	uint64_t timeout_ticks = spdk_get_ticks_hz() * HU_DRAIN_TIMEOUT_SEC;

	/* Check all virtqueues for in-flight IO */
	for (vdev = spdk_vhost_dev_next(NULL); vdev != NULL;
	     vdev = spdk_vhost_dev_next(vdev)) {
		struct spdk_vhost_user_dev *user_dev = to_user_dev(vdev);

		TAILQ_FOREACH(vsession, &user_dev->vsessions, tailq) {
			uint16_t i;

			if (!vsession->started) {
				continue;
			}

			for (i = 0; i < vsession->max_queues; i++) {
				struct spdk_vhost_virtqueue *vq = &vsession->virtqueue[i];

				if (vq->vring.desc == NULL) {
					continue; /* inactive queue */
				}

				if (vq->last_avail_idx != vq->last_used_idx) {
					all_drained = false;
					SPDK_DEBUGLOG(vhost,
						      "hu: drain wait %s vq%u: "
						      "avail=%u used=%u\n",
						      vsession->name, i,
						      vq->last_avail_idx,
						      vq->last_used_idx);
					break;
				}
			}

			if (!all_drained) {
				break;
			}
		}

		if (!all_drained) {
			break;
		}
	}

	if (all_drained) {
		SPDK_NOTICELOG("hu: all vhost in-flight IO drained\n");
	} else if (now - g_hu_drain_start_tsc > timeout_ticks) {
		SPDK_WARNLOG("hu: drain timeout (%ds), proceeding anyway\n",
			     HU_DRAIN_TIMEOUT_SEC);
		all_drained = true; /* force proceed after timeout */
	}

	if (all_drained) {
		spdk_poller_unregister(&g_hu_drain_poller);
		g_hu_drain_state = HU_DRAIN_DONE;

		/* Clear hu_draining flag (Primary will be suspended next) */
		for (vdev = spdk_vhost_dev_next(NULL); vdev != NULL;
		     vdev = spdk_vhost_dev_next(vdev)) {
			struct spdk_vhost_user_dev *user_dev = to_user_dev(vdev);

			TAILQ_FOREACH(vsession, &user_dev->vsessions, tailq) {
				vsession->hu_draining = false;
			}
		}

		spdk_subsystem_primary_drain_io_next(0);
	}

	return SPDK_POLLER_BUSY;
}

/*
 * ===== Unified Entry Points for Subsystem Callbacks =====
 */

int
spdk_vhost_hu_primary_drain_all(void)
{
	struct spdk_vhost_dev *vdev;
	struct spdk_vhost_session *vsession;

	/*
	 * This function is called by both vhost_blk and vhost_scsi subsystems
	 * during drain_io traversal. The first call sets hu_draining on all
	 * sessions and registers the drain poller. The poller calls
	 * spdk_subsystem_primary_drain_io_next(0) when all in-flight IOs
	 * complete (or timeout). The second call (from the other vhost
	 * subsystem) finds drain already done and immediately calls
	 * spdk_subsystem_primary_drain_io_next(0) to advance traversal.
	 */
	if (g_hu_drain_state == HU_DRAIN_DONE) {
		spdk_subsystem_primary_drain_io_next(0);
		return 0;
	}

	if (g_hu_drain_state == HU_DRAIN_IN_PROGRESS) {
		/* Already draining; poller will call next(0) when done */
		return 0;
	}

	g_hu_drain_state = HU_DRAIN_IN_PROGRESS;
	g_hu_drain_start_tsc = spdk_get_ticks();

	/* Set hu_draining flag on all started sessions */
	for (vdev = spdk_vhost_dev_next(NULL); vdev != NULL;
	     vdev = spdk_vhost_dev_next(vdev)) {
		struct spdk_vhost_user_dev *user_dev = to_user_dev(vdev);

		TAILQ_FOREACH(vsession, &user_dev->vsessions, tailq) {
			if (!vsession->started) {
				continue;
			}
			vsession->hu_draining = true;
			SPDK_INFOLOG(vhost, "hu: set hu_draining on session %s\n",
				     vsession->name);
		}
	}

	/* Register drain poller (100us interval for fast completion) */
	g_hu_drain_poller = SPDK_POLLER_REGISTER(vhost_hu_drain_poller, NULL, 100);

	SPDK_NOTICELOG("hu: vhost drain started, waiting for in-flight IO\n");

	return 0;
}

int
spdk_vhost_hu_primary_suspend_all(void)
{
	struct spdk_vhost_dev *vdev;

	/* Reset drain state so next hot upgrade cycle starts fresh */
	g_hu_drain_state = HU_DRAIN_IDLE;
	g_hu_drain_poller = NULL;
	g_hu_drain_start_tsc = 0;

	for (vdev = spdk_vhost_dev_next(NULL); vdev != NULL;
	     vdev = spdk_vhost_dev_next(vdev)) {
		spdk_vhost_extract_mem_fds(vdev);
		spdk_vhost_extract_conn_state(vdev);
		spdk_vhost_cleanup_epoll_before_pause(vdev);
		spdk_vhost_send_fds_to_secondary(vdev);
		vdev->is_hu_suspended = true;
	}
	return 0;
}

int
spdk_vhost_hu_secondary_takeover_all(void)
{
	struct spdk_vhost_dev *vdev;
	for (vdev = spdk_vhost_dev_next(NULL); vdev != NULL;
	     vdev = spdk_vhost_dev_next(vdev)) {
		/*
		 * If user_dev (ctxt) is NULL, the device was rebuilt from
		 * shared state but sessions are not yet available. Skip FD
		 * reception and mem restore — they require valid vsessions.
		 * Session rebuild is a future enhancement.
		 */
		if (vdev->ctxt == NULL) {
			fprintf(stderr, "[HU] vhost: skip takeover for %s (no sessions)\n",
				vdev->name);
			vdev->is_hu_suspended = false;
			continue;
		}
		spdk_vhost_recv_fds_from_primary(vdev);
		struct spdk_vhost_user_dev *udev = to_user_dev(vdev);
		struct spdk_vhost_session *vs;
		TAILQ_FOREACH(vs, &udev->vsessions, tailq)
			spdk_vhost_restore_mem_mapping(vs);
		spdk_vhost_restore_connections(vdev);
		vdev->is_hu_suspended = false;
	}
	return 0;
}
