"""Offline execution contracts before scheduler and invocation ownership moves."""

from __future__ import annotations

import copy
import json
import socket
import tempfile
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from core.exceptions import ConflictError, PipelineError
from core.project_catalog import ProjectSession
from pipeline import PipelineManager
from test_pipeline_unit_contract import review_result, translation_result


class UnitGates:
    def __init__(self, unit_ids, response):
        self.response = response
        self.requests = []
        self.entered = {key: threading.Event() for key in unit_ids}
        self.release = {key: threading.Event() for key in unit_ids}

    def respond(self, request):
        self.requests.append(request)
        self.entered[request.unit_id].set()
        if not self.release[request.unit_id].wait(5):
            raise AssertionError("Offline test did not release provider")
        return self.response(request)

    translate = review = respond

    def release_all(self):
        for event in self.release.values():
            event.set()


class RecordedExecutor(ThreadPoolExecutor):
    """Real worker threads; expose submitted Futures to the test fixture only."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tasks = []

    def submit(self, fn, *args, **kwargs):
        future = super().submit(fn, *args, **kwargs)
        self.tasks.append((args[0], future))
        return future


class ManualTimer:
    def __init__(self, interval, function, args=(), kwargs=None):
        self.interval, self.function, self.args = interval, function, args
        self.kwargs = kwargs or {}
        self.started = False
        self.cancelled = False
        self.daemon = False

    def start(self):
        self.started = True

    def cancel(self):
        self.cancelled = True

    def fire(self):
        # Explicitly allow a stale callback already dispatched before cancel.
        self.function(*self.args, **self.kwargs)


class ImmediateExecutor:
    def __init__(self, **kwargs):
        pass

    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, *, wait):
        pass


class RejectingExecutor(ImmediateExecutor):
    def submit(self, fn, *args):
        raise RuntimeError("offline submit rejected")


class PipelineExecutionContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.executors = []
        self.timers = []
        self.cleanups = []
        self.addCleanup(self.finish_workers)
        self.patches = []
        self.install_patch(patch.object(socket.socket, "connect",
            side_effect=AssertionError("Execution contracts forbid network")))
        self.install_patch(patch("pipeline.ThreadPoolExecutor", side_effect=self.make_executor))
        self.install_patch(patch("pipeline.threading.Timer", side_effect=self.make_timer))

    def install_patch(self, patcher):
        patcher.start()
        self.patches.append(patcher)

    def make_executor(self, **kwargs):
        executor = RecordedExecutor(**kwargs)
        self.executors.append(executor)
        return executor

    def make_timer(self, *args, **kwargs):
        timer = ManualTimer(*args, **kwargs)
        self.timers.append(timer)
        return timer

    def finish_workers(self):
        try:
            for manager, translator, reviewer in self.cleanups:
                translator.release_all()
                reviewer.release_all()
            # Futures are fixture observations; callbacks registered here run
            # after the manager's existing durable completion callback.
            for executor in self.executors:
                for _, future in executor.tasks:
                    done = threading.Event()
                    future.add_done_callback(lambda _, event=done: event.set())
                    self.assertTrue(done.wait(5))
            for manager, _, _ in self.cleanups:
                manager.close()
        finally:
            for patcher in reversed(self.patches):
                patcher.stop()

    def fixture(self, *, concurrency=2, manager=None):
        manager = manager or PipelineManager(Path(self.tmp.name) / "project")
        state = manager.create_project("Hello.\n\nGoodbye.", max_concurrency=concurrency)
        ids = [unit["id"] for unit in state["units"]]
        self.assertEqual(len(ids), 2)
        translator = UnitGates(ids, translation_result)
        reviewer = UnitGates(ids, review_result)
        manager.translation_provider, manager.review_provider = translator, reviewer
        self.cleanups.append((manager, translator, reviewer))
        return manager, ids, translator, reviewer

    def completed(self, unit_id):
        future = next(future for executor in self.executors for key, future in executor.tasks
                      if key == unit_id)
        done = threading.Event()
        future.add_done_callback(lambda _: done.set())
        return done

    def disk(self, manager):
        return json.loads(manager.state_path.read_text(encoding="utf-8"))

    def test_two_real_workers_share_complete_run_scope_until_both_finish(self):
        manager, ids, translator, reviewer = self.fixture()
        manager.start(ids)
        for key in ids:
            self.assertTrue(translator.entered[key].wait(5))
        first_done, second_done = map(self.completed, ids)
        run_id = manager.snapshot()["run"]["run_id"]
        translator.release[ids[0]].set()
        self.assertTrue(reviewer.entered[ids[0]].wait(5))
        reviewer.release[ids[0]].set()
        self.assertTrue(first_done.wait(5))
        run = manager.snapshot()["run"]
        self.assertEqual((run["run_id"], run["unit_ids"], run["completed_unit_ids"]),
                         (run_id, ids, [ids[0]]))
        self.assertTrue(run["running"])
        self.assertEqual(self.disk(manager)["run"], run)
        self.assertEqual(manager.get_unit(ids[1])["status"], "translating")
        translator.release[ids[1]].set()
        self.assertTrue(reviewer.entered[ids[1]].wait(5))
        reviewer.release[ids[1]].set()
        self.assertTrue(second_done.wait(5))
        self.assertEqual([manager.get_unit(key)["status"] for key in ids], ["passed", "passed"])
        self.assertFalse(manager.snapshot()["run"]["running"])
        self.assertEqual(len([event for event in manager.snapshot()["events"]
                              if event["type"] == "run_finished"]), 1)

    def test_stop_cancels_queued_unit_before_provider_and_discards_active_translation(self):
        manager, ids, translator, reviewer = self.fixture(concurrency=1)
        manager.start(ids)
        self.assertTrue(translator.entered[ids[0]].wait(5))
        active_done = self.completed(ids[0])
        stopped = manager.stop()
        self.assertEqual(stopped["run"]["status"], "stopping")
        self.assertEqual([manager.get_unit(key)["translation_revision"] for key in ids], [0, 0])
        translator.release[ids[0]].set()
        self.assertTrue(active_done.wait(5))
        self.assertEqual([request.unit_id for request in translator.requests], [ids[0]])
        self.assertEqual(reviewer.requests, [])
        self.assertEqual([manager.get_unit(key)["status"] for key in ids], ["cancelled", "cancelled"])
        self.assertEqual(manager.snapshot()["run"]["status"], "cancelled")
        self.assertEqual(self.disk(manager)["units"], manager.snapshot()["units"])

    def test_stop_after_durable_translation_prevents_automatic_review(self):
        manager, ids, translator, reviewer = self.fixture()
        save = manager.store.save
        stop_once = threading.Event()

        def observed_save(state):
            save(state)
            if state["units"][0]["status"] == "waiting_review" and not stop_once.is_set():
                stop_once.set()
                manager.stop()  # Same-thread RLock, exactly between stages.

        self.install_patch(patch.object(manager.store, "save", side_effect=observed_save))
        manager.start([ids[0]])
        self.assertTrue(translator.entered[ids[0]].wait(5))
        done = self.completed(ids[0])
        translator.release[ids[0]].set()
        self.assertTrue(done.wait(5))
        unit = manager.get_unit(ids[0])
        self.assertTrue(stop_once.is_set())
        self.assertEqual((unit["translation"], unit["translation_revision"], unit["status"]),
                         ("你好。", 1, "cancelled"))
        self.assertEqual(reviewer.requests, [])
        self.assertEqual(self.disk(manager)["units"][0], unit)

    def test_grace_timeout_keeps_old_unit_busy_and_late_callback_cannot_end_new_run(self):
        manager, ids, translator, reviewer = self.fixture()
        manager.start([ids[0]])
        self.assertTrue(translator.entered[ids[0]].wait(5))
        old_done = self.completed(ids[0])
        old_run = manager.snapshot()["run"]["run_id"]
        manager.stop()
        timer = self.timers[-1]
        self.assertEqual((timer.interval, timer.started, timer.daemon), (5.0, True, True))
        timer.fire()
        expired = manager.snapshot()["run"]
        self.assertEqual((expired["status"], expired["running"]), ("cancelled", False))
        self.assertTrue(expired["stop_timeout_at"])
        with self.assertRaises(ConflictError):
            manager.start([ids[0]])
        manager.start([ids[1]])
        self.assertTrue(translator.entered[ids[1]].wait(5))
        new_done = self.completed(ids[1])
        new_run = manager.snapshot()["run"]["run_id"]
        self.assertNotEqual(new_run, old_run)
        timer.fire()  # Stale timeout also cannot affect the current run.
        translator.release[ids[0]].set()
        self.assertTrue(old_done.wait(5))
        current = manager.snapshot()
        self.assertEqual((current["run"]["run_id"], current["run"]["unit_ids"],
                          current["run"]["completed_unit_ids"], current["run"]["running"]),
                         (new_run, [ids[1]], [], True))
        self.assertEqual((manager.get_unit(ids[0])["status"], manager.get_unit(ids[0])["translation"]),
                         ("cancelled", ""))
        self.assertEqual(self.disk(manager)["run"], current["run"])
        translator.release[ids[1]].set()
        self.assertTrue(reviewer.entered[ids[1]].wait(5))
        reviewer.release[ids[1]].set()
        self.assertTrue(new_done.wait(5))
        self.assertEqual(manager.get_unit(ids[1])["status"], "passed")
        self.assertEqual(manager.snapshot()["run"]["completed_unit_ids"], [ids[1]])

    def test_submit_failure_restores_revision_and_releases_unit_without_final_save(self):
        manager, ids, translator, reviewer = self.fixture()
        before = self.disk(manager)
        self.install_patch(patch("pipeline.ThreadPoolExecutor", RejectingExecutor))
        with self.assertRaisesRegex(PipelineError, "任务入队失败：offline submit rejected"):
            manager.start([ids[0]])
        unit = manager.get_unit(ids[0])
        self.assertEqual((unit["status"], unit["translation_revision"], unit["translation_attempts"]),
                         ("needs_action", 0, 1))
        self.assertEqual(unit["review"]["issues"][0]["rule"], "scheduler_error")
        self.assertEqual((translator.requests, reviewer.requests), ([], []))
        self.assertEqual(self.disk(manager), before)
        manager.close()  # Failure did not leave a live task reservation.

    def test_completed_future_callback_observes_full_scope_before_first_submission(self):
        manager, ids, translator, reviewer = self.fixture()
        translator.release_all()
        reviewer.release_all()
        saved = []
        save = manager.store.save

        def observed_save(state):
            save(state)
            saved.append(copy.deepcopy(state))

        self.install_patch(patch.object(manager.store, "save", side_effect=observed_save))
        self.install_patch(patch("pipeline.ThreadPoolExecutor", ImmediateExecutor))
        result = manager.start(ids)
        self.assertEqual(result["run"]["unit_ids"], ids)
        self.assertEqual(result["run"]["completed_unit_ids"], ids)
        self.assertFalse(result["run"]["running"])
        first_complete = next(state for state in saved if state["run"]["completed_unit_ids"] == [ids[0]])
        self.assertTrue(first_complete["run"]["running"])
        self.assertEqual(first_complete["run"]["unit_ids"], ids)
        self.assertEqual([unit["status"] for unit in result["units"]], ["passed", "passed"])
        self.assertEqual((len(translator.requests), len(reviewer.requests)), (2, 2))
        self.assertEqual(self.disk(manager)["run"], result["run"])

    def test_close_and_session_delete_reject_live_and_retired_worker_then_delete_safely(self):
        root = Path(self.tmp.name)
        session = ProjectSession(root / "book", settings_dir=root / "settings")
        project = session.create_named_project("offline execution")
        # Only fixture injection needs the current object; assertions and
        # lifecycle operations use the public manager/session APIs.
        manager, ids, translator, reviewer = self.fixture(manager=session._manager)
        manager.start([ids[0]])
        self.assertTrue(translator.entered[ids[0]].wait(5))
        done = self.completed(ids[0])
        for retired in (False, True):
            with self.subTest(retired=retired):
                if retired:
                    manager.stop()
                    self.timers[-1].fire()
                with self.assertRaises(ConflictError):
                    manager.close()
                with self.assertRaises(ConflictError):
                    session.delete_project(project["id"])
                self.assertTrue(manager.state_path.exists())
                self.assertEqual(session.snapshot()["current_project"]["id"], project["id"])
        translator.release[ids[0]].set()
        self.assertTrue(done.wait(5))
        self.assertEqual(reviewer.requests, [])
        result = session.delete_project(project["id"])
        self.assertTrue(result["deleted"])
        self.assertFalse(manager.state_path.parent.exists())
        self.assertEqual(session.list_projects(), [])
        with self.assertRaises(ConflictError):
            manager.start([ids[1]])

    def test_stop_during_review_discards_pass_but_keeps_translation_and_revision(self):
        manager, ids, translator, reviewer = self.fixture()
        manager.start([ids[0]])
        self.assertTrue(translator.entered[ids[0]].wait(5))
        done = self.completed(ids[0])
        translator.release[ids[0]].set()
        self.assertTrue(reviewer.entered[ids[0]].wait(5))
        manager.stop()
        reviewer.release[ids[0]].set()
        self.assertTrue(done.wait(5))
        unit = manager.get_unit(ids[0])
        self.assertEqual((unit["status"], unit["translation"], unit["translation_revision"]),
                         ("cancelled", "你好。", 1))
        self.assertIsNone(unit["review"])
        self.assertEqual((len(translator.requests), len(reviewer.requests)), (1, 1))
        self.assertFalse(any(event["type"] == "review_passed" for event in manager.snapshot()["events"]))
        self.assertEqual(self.disk(manager)["units"][0], unit)


if __name__ == "__main__":
    unittest.main()
