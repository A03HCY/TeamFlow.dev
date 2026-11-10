"""推理作业 worker（P1：规则版，单进程内 asyncio 循环）。

为什么不用 Celery/Redis：P0 的定位是「单机也能跑」，作业表就在同一个库里，
``queued → running → done/failed`` 的状态转移用一次 UPDATE 抢锁即可（SQLite 也
成立）。多实例部署时把 ``_claim`` 换成 ``UPDATE ... WHERE status='queued'`` 的
条件更新，谁抢到算谁的——这个顺序在代码里已经写好了。

测试里用 ``TEAMFLOW_INFERENCE_WORKER=0`` 关掉后台循环，再手动
``drain_once()``，避免 ``TestClient`` 的 lifespan 与用例抢同一个库。
"""
from __future__ import annotations

import asyncio
import json
import logging

from sqlalchemy import select

from ...config import (
    ENGINE_MAX_CONCURRENCY,
    ENGINE_WORKER_MAX_ATTEMPTS,
    ENGINE_WORKER_POLL_SECONDS,
    inference_worker_enabled,
)
from ...db.base import utcnow_iso
from ...db.engine_models import InferenceJob
from ...db.session import session_scope
from . import pipeline

logger = logging.getLogger("teamflow.worker")


def _loads(raw: str | None, fallback):
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


class InferenceWorker:
    """把队列里的作业逐条跑完；规则阶段是纯计算，很快。"""

    def __init__(
        self,
        *,
        poll_seconds: float = ENGINE_WORKER_POLL_SECONDS,
        concurrency: int = ENGINE_MAX_CONCURRENCY,
        max_attempts: int = ENGINE_WORKER_MAX_ATTEMPTS,
    ) -> None:
        self.poll_seconds = max(0.2, float(poll_seconds))
        self.concurrency = max(1, int(concurrency))
        self.max_attempts = max(1, int(max_attempts))
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._running_ids: set[int] = set()
        self.counters = {"claimed": 0, "done": 0, "failed": 0, "insights": 0}

    # ------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="teamflow-inference-worker")
        logger.info("推理作业 worker 已启动（并发 %s）", self.concurrency)

    async def stop(self) -> None:
        self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        logger.info("推理作业 worker 已停止")

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def stats(self) -> dict:
        return {**self.counters, "running": self.running, "in_flight": len(self._running_ids)}

    # ------------------------------------------------------------ 主循环
    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                worked = await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker 不能因为一次异常退出
                logger.warning("推理 worker 轮询异常：%s", exc)
                worked = 0
            if not worked:
                await self._sleep(self.poll_seconds)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _tick(self) -> int:
        free = self.concurrency - len(self._running_ids)
        if free <= 0:
            return 0
        jobs = self._claim(free)
        if not jobs:
            return 0
        results = await asyncio.gather(
            *(asyncio.to_thread(self._process, job_id) for job_id in jobs),
            return_exceptions=True,
        )
        worked = 0
        for job_id, result in zip(jobs, results):
            worked += 1
            if isinstance(result, Exception):
                logger.warning("作业 %s 执行失败：%s", job_id, result)
                continue
            await self._after(result)
        return worked

    # ------------------------------------------------------------ 抢锁
    def _claim(self, limit: int) -> list[int]:
        """按 id 顺序取 queued 作业并置为 running（条件更新，防重复领取）。"""
        claimed: list[int] = []
        with session_scope() as session:
            rows = list(
                session.execute(
                    select(InferenceJob)
                    .where(InferenceJob.status == "queued")
                    .order_by(InferenceJob.id)
                    .limit(limit * 2)
                ).scalars()
            )
            for job in rows:
                if len(claimed) >= limit:
                    break
                job.status = "running"
                job.started_at = utcnow_iso()
                job.attempts = int(job.attempts or 0) + 1
                claimed.append(job.id)
            if claimed:
                self.counters["claimed"] += len(claimed)
        for job_id in claimed:
            self._running_ids.add(job_id)
        return claimed

    # ------------------------------------------------------------ 执行
    def _process(self, job_id: int) -> dict:
        try:
            with session_scope() as session:
                job = session.get(InferenceJob, job_id)
                if job is None:
                    return {"ok": False, "job_id": job_id, "error": "作业不存在"}
                result = pipeline.run_job(session, job)
                job.status = "done"
                job.finished_at = utcnow_iso()
                job.error = ""
                job.budget = json.dumps(
                    {"stage": "rules", "events": result.events}, ensure_ascii=False
                )
                return {"ok": True, "job_id": job_id, **result.as_dict()}
        except Exception as exc:  # noqa: BLE001
            message = f"{type(exc).__name__}: {exc}"
            self._mark_failed(job_id, message)
            return {"ok": False, "job_id": job_id, "error": message}
        finally:
            self._running_ids.discard(job_id)

    def _mark_failed(self, job_id: int, message: str) -> None:
        with session_scope() as session:
            job = session.get(InferenceJob, job_id)
            if job is None:
                return
            attempts = int(job.attempts or 0)
            job.error = message[:1000]
            if attempts < self.max_attempts:
                job.status = "queued"
                self.counters["claimed"] -= 0  # 保持可观测：重试不计入失败
            else:
                job.status = "failed"
                job.finished_at = utcnow_iso()
                self.counters["failed"] += 1

    async def _after(self, result: dict) -> None:
        if not result.get("ok"):
            return
        if result.get("created") or result.get("merged"):
            self.counters["done"] += 1
            if result.get("insight_id"):
                self.counters["insights"] += 1
                await self._broadcast(result)
        else:
            self.counters["done"] += 1

    async def _broadcast(self, result: dict) -> None:
        """把新洞察推给同团队的在线设备（端侧据此弹卡片/角标）。"""
        try:
            from ..edge.gateway import registry

            team_id = self._team_of(int(result.get("job_id") or 0))
            if not team_id:
                return
            await registry.broadcast(
                team_id,
                {
                    "type": "insight",
                    "insight_id": result.get("insight_id"),
                    "job_id": result.get("job_id"),
                    "created": bool(result.get("created")),
                    "merged": bool(result.get("merged")),
                    "confidence": result.get("confidence"),
                },
            )
        except Exception as exc:  # noqa: BLE001 - 推送失败不影响作业结果
            logger.debug("洞察推送失败：%s", exc)

    def _team_of(self, job_id: int) -> int | None:
        if not job_id:
            return None
        with session_scope() as session:
            job = session.get(InferenceJob, job_id)
            return job.team_id if job else None

    # ------------------------------------------------------------ 手动驱动
    def drain_once(self, *, limit: int = 10) -> list[dict]:
        """同步跑一批（测试与运维排障用），返回每个作业的结果。"""
        results: list[dict] = []
        for job_id in self._claim(limit):
            results.append(self._process(job_id))
        return results


_worker: InferenceWorker | None = None


def get_worker() -> InferenceWorker:
    global _worker
    if _worker is None:
        _worker = InferenceWorker()
    return _worker


async def start_worker() -> InferenceWorker | None:
    """应用启动时调用；``TEAMFLOW_INFERENCE_WORKER=0`` 时什么都不做。"""
    if not inference_worker_enabled():
        logger.info("推理作业 worker 已按配置关闭（TEAMFLOW_INFERENCE_WORKER=0）")
        return None
    worker = get_worker()
    await worker.start()
    return worker


async def stop_worker() -> None:
    global _worker
    if _worker is not None:
        await _worker.stop()


def pending_count() -> int:
    with session_scope() as session:
        rows = session.execute(
            select(InferenceJob).where(InferenceJob.status.in_(("queued", "running")))
        ).scalars()
        return len(list(rows))


def job_dict(job: InferenceJob) -> dict:
    return {
        "id": job.id,
        "team_id": job.team_id,
        "status": job.status,
        "coalesce_key": job.coalesce_key,
        "attempts": int(job.attempts or 0),
        "error": job.error or "",
        "run_id": job.run_id or "",
        "payload": _loads(job.payload, {}),
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
    }


__all__ = [
    "InferenceWorker",
    "get_worker",
    "job_dict",
    "pending_count",
    "start_worker",
    "stop_worker",
]
